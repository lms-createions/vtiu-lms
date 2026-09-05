# app.py - My LMS — Clean Production Version with Robust Database Initialization

import os
import logging
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, flash, request, abort, jsonify, send_from_directory, current_app, g
from werkzeug.utils import secure_filename
import time
import signal

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# ===== Extensions & Config =====
from flask_login import LoginManager, login_required, logout_user, current_user
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect, CSRFError, generate_csrf
from utils.extensions import db, mail, socketio
from config import Config

# ===== Flask App =====
app = Flask(__name__)
app.config.from_object(Config)

# Initialize extensions ONCE
db.init_app(app)
migrate = Migrate(app, db)
mail.init_app(app)
socketio.init_app(app, cors_allowed_origins="*", async_mode='threading')
csrf = CSRFProtect(app)

# ===== Logging =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== Configuration =====
# Check if we're in production (Render or Railway deployment)
IS_PRODUCTION = bool(
    app.config.get("IS_PRODUCTION")
    or os.environ.get("IS_PRODUCTION") in ("1", "true", "True")
    or os.environ.get("FLASK_ENV", "").lower() == "production"
    or os.environ.get("RENDER") == "true"  # Render sets this automatically
    or "render.com" in os.environ.get("RENDER_EXTERNAL_URL", "")
    or os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
    or os.environ.get("RAILWAY_PROJECT_ID")
    or os.environ.get("RAILWAY_SERVICE_ID")
)

logger.info(f"🌍 Environment: {'PRODUCTION (Render)' if IS_PRODUCTION else 'LOCAL DEVELOPMENT'}")

# ===== Memory Management =====
import gc
import psutil
from threading import Thread
import time

def monitor_memory_usage():
    """Monitor memory usage and perform cleanup to prevent worker timeouts"""
    if not IS_PRODUCTION:
        return  # Only run in production
    
    process = psutil.Process()
    memory_limit_mb = app.config.get('MEMORY_LIMIT_MB', 512)
    
    def cleanup_task():
        while True:
            try:
                memory_info = process.memory_info()
                memory_mb = memory_info.rss / 1024 / 1024
                
                if memory_mb > memory_limit_mb * 0.6:  # Lowered from 80% to 60% for more aggressive cleanup
                    logger.warning(f"🧹 High memory usage detected: {memory_mb:.1f}MB - Performing cleanup")
                    
                    # Force garbage collection
                    gc.collect()
                    
                    # Close idle database connections
                    try:
                        db.engine.dispose()
                        logger.info("🗄️ Database connections cleaned up")
                    except Exception as e:
                        logger.error(f"Error cleaning up database connections: {e}")
                    
                    memory_after = process.memory_info().rss / 1024 / 1024
                    logger.info(f"✅ Memory after cleanup: {memory_after:.1f}MB")
                
                time.sleep(app.config.get('CLEANUP_INTERVAL', 180))  # Reduced from 300 to 180
                
            except Exception as e:
                logger.error(f"Error in memory monitoring: {e}")
                time.sleep(60)  # Wait 1 minute before retrying
    
    # Start monitoring in background thread
    monitor_thread = Thread(target=cleanup_task, daemon=True)
    monitor_thread.start()
    logger.info("🧠 Memory monitoring started")

# Start memory monitoring if in production
if IS_PRODUCTION:
    try:
        # Temporarily disabled to test if monitoring is causing timeouts
        logger.info("🔧 Memory monitoring temporarily disabled for testing")
        # monitor_memory_usage()
        
        # Add memory leak detection
        import tracemalloc
        tracemalloc.start()
        logger.info("🔍 Memory leak detection started")
        
    except ImportError:
        logger.warning("⚠️ psutil not available - memory monitoring disabled")
    except Exception as e:
        logger.error(f"❌ Failed to start memory monitoring: {e}")

# ===== Request Timeout Middleware =====
@app.before_request
def before_request():
    """Monitor request start time and memory usage to prevent timeouts"""
    g.start_time = time.time()
    
    # Check for existing long-running requests
    elapsed = time.time() - g.start_time
    if elapsed > 55:  # Kill requests after 55 seconds
        logger.error(f"⏰ REQUEST TIMEOUT: {request.method} {request.path} after {elapsed:.2f}s - Terminating to prevent worker kill")
        abort(408, "Request timeout - Please try again")
    
    # Log memory usage at start of each request
    try:
        import psutil
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        logger.info(f"🔍 Request start: {request.method} {request.path} - Memory: {memory_mb:.1f}MB")
    except ImportError:
        pass

@app.after_request  
def after_request(response):
    """Check request duration and log slow requests"""
    if hasattr(g, 'start_time'):
        duration = time.time() - g.start_time
        
        # Warn about slow requests
        if duration > 30:  # Warn for requests over 30 seconds
            logger.warning(f"🐌 SLOW REQUEST: {request.method} {request.path} took {duration:.2f}s - This may cause worker timeout!")
        elif duration > 10:  # Log requests taking longer than 10 seconds
            logger.warning(f"🐌 Slow request detected: {request.method} {request.path} took {duration:.2f}s")
        
        # Log memory usage at end of request
        try:
            import psutil
            import tracemalloc
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            
            # Get memory snapshot if tracemalloc is running
            snapshot_info = ""
            if tracemalloc.is_tracing():
                current, peak = tracemalloc.get_traced_memory()
                snapshot_info = f" - Traced: {current/1024/1024:.1f}MB / {peak/1024/1024:.1f}MB"
            
            logger.info(f"✅ Request end: {request.method} {request.path} - Duration: {duration:.2f}s - Memory: {memory_mb:.1f}MB{snapshot_info}")
            
            # Log memory snapshots every 10 requests
            if not hasattr(app, '_request_counter'):
                app._request_counter = 0
            app._request_counter += 1
            
            if app._request_counter % 10 == 0:
                if tracemalloc.is_tracing():
                    snapshot = tracemalloc.take_snapshot()
                    top_stats = snapshot.statistics('lineno')[:5]
                    logger.warning(f"🔍 Top 5 memory allocations after {app._request_counter} requests:")
                    for i, stat in enumerate(top_stats, 1):
                        logger.warning(f"  {i}. {stat}")
                        
        except ImportError:
            pass
    
    return response

# ===== Helper Function to Initialize Database =====
def initialize_database():
    """
    Initialize database tables and create SuperAdmin
    This function can be called from multiple places:
    1. Automatic initialization on app startup (production)
    2. Manual initialization via /init-db route
    3. Force initialization via /init-all-tables route
    """
    try:
        logger.info("=" * 60)
        logger.info("🔧 DATABASE INITIALIZATION STARTING...")
        logger.info("=" * 60)
        
        # Import ALL models to ensure they're registered with SQLAlchemy
        logger.info("📦 Importing all models...")
        from models import (
            # Core user models
            User, Admin, StudentProfile, TeacherProfile,
            
            # Fee and finance models
            StudentFeeTransaction, StudentFeeBalance, ProgrammeFeeStructure,
            
            # Notification models
            Notification, NotificationRecipient, NotificationPreference,
            
            # Course and academic models
            Course, CourseLimit, StudentCourseRegistration, TimetableEntry,
            CourseMaterial, CourseAssessmentScheme,
            
            # Assignment and quiz models
            Assignment, AssignmentSubmission, Quiz, StudentQuizSubmission,
            Question, Option, StudentAnswer, QuizAttempt,
            
            # Exam models
            Exam, ExamQuestion, ExamOption, ExamSet, ExamSetQuestion,
            ExamAttempt, ExamSubmission, ExamAnswer, ExamTimetableEntry,
            
            # Grading models
            GradingScale, StudentCourseGrade, SemesterResultRelease,
            
            # Calendar and schedule models
            AcademicCalendar, AcademicYear, SchoolSettings,
            
            # Appointment models
            AppointmentSlot, AppointmentBooking,
            
            # Meeting and communication models
            Meeting, Recording, Conversation, ConversationParticipant,
            Message, MessageReaction,
            
            # Assessment models
            TeacherCourseAssignment, TeacherAssessment,
            TeacherAssessmentAnswer, TeacherAssessmentPeriod,
            TeacherAssessmentQuestion,
            
            # Other models
            ProgrammeCohort, StudentPromotion,
            PasswordResetRequest, PasswordResetToken
        )
        
        # Import admission models
        from admissions.models import (
            Applicant, Application, ApplicationDocument, AdmissionVoucher,
            ApplicationResult, ApplicationPayment
        )
        logger.info("✅ All models imported successfully")

        # Keep existing PostgreSQL databases compatible with newly added model
        # columns. db.create_all() does not alter existing tables.
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        admin_columns = {column["name"] for column in inspector.get_columns("admin")}
        if "notes" not in admin_columns:
            logger.info("🔧 Adding missing admin.notes column...")
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE admin ADD COLUMN notes TEXT"))
            logger.info("✅ admin.notes column added")

        student_profile_columns = {
            column["name"]
            for column in inspector.get_columns("student_profile")
        }
        with db.engine.begin() as connection:
            if "vetting_status" not in student_profile_columns:
                logger.info("🔧 Adding missing student_profile.vetting_status column...")
                connection.execute(text(
                    "ALTER TABLE student_profile "
                    "ADD COLUMN vetting_status VARCHAR(20) DEFAULT 'pending'"
                ))
            if "rejection_reason" not in student_profile_columns:
                logger.info("🔧 Adding missing student_profile.rejection_reason column...")
                connection.execute(text(
                    "ALTER TABLE student_profile ADD COLUMN rejection_reason TEXT"
                ))
        
        # Create all tables using db.create_all() - this is safest method
        logger.info("🔨 Creating all database tables...")
        try:
            db.create_all()
            logger.info("✅ db.create_all() completed successfully")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                logger.info("✅ Some tables/indexes already exist - continuing...")
            else:
                logger.warning(f"⚠️ db.create_all() warning: {e}")

        # Repair partially initialized databases. A failed create_all() can
        # leave later model tables absent even though their models are loaded.
        logger.info("🔍 Checking every imported model table...")
        existing_tables = set(inspect(db.engine).get_table_names())
        for table in db.metadata.sorted_tables:
            if table.name in existing_tables:
                continue
            try:
                table.create(db.engine, checkfirst=True)
                existing_tables.add(table.name)
                logger.info(f"  ✓ {table.name} (created)")
            except Exception as e:
                logger.warning(f"  ⚠️ {table.name}: {e}")
                db.session.rollback()
        
        # Double-check critical tables exist by trying to create them explicitly
        logger.info("🔍 Verifying critical tables...")
        critical_tables = [
            (User, "user"),
            (Admin, "admin"),
            (StudentProfile, "student_profile"),
            (ProgrammeFeeStructure, "programme_fee_structure"),
            (StudentFeeBalance, "student_fee_balance"),
            (StudentFeeTransaction, "student_fee_transaction"),
            (Notification, "notifications"),
            (NotificationRecipient, "notification_recipients"),
            (NotificationPreference, "notification_preferences"),
            (Course, "course"),
            (Assignment, "assignment"),
            (Quiz, "quiz"),
            (Exam, "exam"),
            (StudentCourseRegistration, "student_course_registration"),
            (CourseMaterial, "course_material"),
            (TimetableEntry, "timetable_entry"),
            (TeacherProfile, "teacher_profile"),
            # Admission models
            (Applicant, "applicant"),
            (Application, "application"),
            (ApplicationDocument, "application_document"),
            (AdmissionVoucher, "admission_voucher"),
            (ApplicationResult, "application_result"),
            (ApplicationPayment, "application_payment"),
        ]
        
        for model, table_name in critical_tables:
            try:
                model.__table__.create(db.engine, checkfirst=True)
                logger.info(f"  ✓ {table_name}")
            except Exception as e:
                if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                    logger.info(f"  ✓ {table_name} (already exists)")
                else:
                    logger.warning(f"  ⚠️ {table_name}: {e}")
                    # Rollback any failed transaction
                    db.session.rollback()
        
        # Check how many tables were created
        inspector = inspect(db.engine)
        all_tables = inspector.get_table_names()
        logger.info(f"📊 Total tables in database: {len(all_tables)}")
        
        # Create SuperAdmin if it doesn't exist
        logger.info("👤 Checking for SuperAdmin account...")
        existing_admin = Admin.query.filter_by(username='SuperAdmin').first()
        
        if not existing_admin:
            logger.info("🔧 Creating SuperAdmin account...")
            admin = Admin(
                username='SuperAdmin',
                admin_id='SUP001',
                email='admin@lms.com'
            )
            admin.set_password('Password123')
            Admin.apply_superadmin_preset(admin)
            db.session.add(admin)
            db.session.commit()
            logger.info("✅ SuperAdmin created successfully")
            logger.info("   Username: SuperAdmin")
            logger.info("   Password: Password123")
            logger.info("   Admin ID: SUP001")
        else:
            logger.info("✅ SuperAdmin already exists")
        
        logger.info("=" * 60)
        logger.info("✅ DATABASE INITIALIZATION COMPLETE")
        logger.info("=" * 60)
        
        return True, "Database initialized successfully"
        
    except Exception as e:
        error_msg = f"Database initialization error: {str(e)}"
        logger.error("=" * 60)
        logger.error("❌ DATABASE INITIALIZATION FAILED")
        logger.error(error_msg)
        logger.error("=" * 60)
        import traceback
        logger.error(traceback.format_exc())
        return False, error_msg

# ===== Auto-Initialize Database on Startup (Production Only) =====
if IS_PRODUCTION:
    logger.info("🚀 Production environment detected - auto-initializing database...")
    with app.app_context():
        success, message = initialize_database()
        if success:
            # Database recreation trigger - 2026-02-14 08:47
            logger.info("🔄 Database recreation triggered after truncate cascade")
        else:
            logger.error(f"⚠️ Auto-initialization failed: {message}")
            logger.error("💡 You can manually initialize by visiting /init-db")
else:
    logger.info("🏠 Local development environment - skipping auto-initialization")
    logger.info("💡 Use /init-db route to initialize database manually")

# ===== Eventlet Configuration =====
try:
    import eventlet
    if IS_PRODUCTION:
        eventlet.monkey_patch()
    SOCKETIO_ASYNC_MODE = "eventlet"
except ImportError:
    SOCKETIO_ASYNC_MODE = "threading"

logger.info(f"🔌 SocketIO mode: {SOCKETIO_ASYNC_MODE}")

# ===== Login Manager =====
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin.admin_login'

@login_manager.user_loader
def load_user(user_id):
    """
    Load user by ID for Flask-Login
    Handles both Admin and User types with proper ID format:
    - Admin: "admin:public_id" 
    - User: "user:public_id" or numeric ID (legacy)
    """
    try:
        if user_id.startswith('admin:'):
            # Extract public_id from "admin:public_id" format
            public_id = user_id.split(':', 1)[1]
            from models import Admin
            return Admin.query.filter_by(public_id=public_id).first()
        elif user_id.startswith('user:'):
            # Extract public_id from "user:public_id" format
            public_id = user_id.split(':', 1)[1]
            from models import User
            return User.query.filter_by(public_id=public_id).first()
        else:
            # Legacy numeric ID support
            from models import User
            return User.query.get(int(user_id))
    except Exception as e:
        logger.error(f"Error loading user {user_id}: {e}")
        return None

# ===== CSRF Protection =====
@app.before_request
def make_csrf_token_available():
    """Make CSRF token available in all templates"""
    generate_csrf()

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Handle CSRF errors gracefully"""
    flash('Security token expired. Please try again.', 'error')
    return redirect(url_for('home'))

# ===== Template Filters =====
def _start_year_filter(value):
    """Extract start year from academic year string (e.g., '2024/2025' -> '2024')"""
    if isinstance(value, str) and '/' in value:
        return value.split('/')[0]
    return value

app.jinja_env.filters['start_year'] = _start_year_filter

# ===== Basic Routes =====
@app.route('/')
def home():
    """Home page route with enhanced error handling"""
    try:
        return render_template('home.html')
    except Exception as e:
        logger.exception("Template error on /: %s", e)
        return f"""
        <h1>⚠️ Template Rendering Error</h1>
        <p>Error: {str(e)}</p>
        <p>If you just deployed, try initializing the database:</p>
        <ul>
            <li><a href="/init-db">Initialize Database</a></li>
            <li><a href="/health">Check Health</a></li>
        </ul>
        """, 500

@app.route('/portal')
def select_portal():
    """Portal selection page"""
    return render_template('portal_selection.html')

@app.route('/logout')
@login_required
def logout():
    """Global logout route - redirects to appropriate portal"""
    from flask_login import logout_user
    logout_user()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('select_portal'))

@app.route('/portal/<portal>')
def redirect_to_portal(portal):
    """Redirect to specific portal"""
    mapping = {
        'exams': 'exam.exam_login',
        'teachers': 'teacher.teacher_login',
        'students': 'student.student_login',
        'vclass': 'vclass.vclass_login'
    }
    key = portal.lower()
    if key not in mapping:
        abort(404)
    return redirect(url_for(mapping[key]))

@app.route('/health')
def health():
    """
    Lightweight health check for load balancers and Render
    Also checks database connectivity
    """
    try:
        # Check database connection
        db_status = "connected"
        table_count = 0
        try:
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            table_count = len(inspector.get_table_names())
        except Exception as e:
            db_status = f"error: {str(e)}"
        
        return jsonify({
            'status': 'ok',
            'service': 'lms',
            'environment': 'production' if IS_PRODUCTION else 'development',
            'database': db_status,
            'tables': table_count,
            'timestamp': datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

# ===== Database Initialization Routes =====

@app.route('/init-db')
def init_database_route():
    """
    Manual database initialization route
    Works in both development and production
    Safe to call multiple times (won't duplicate data)
    """
    try:
        with app.app_context():
            success, message = initialize_database()
            
            if success:
                return jsonify({
                    'status': 'success',
                    'message': message,
                    'next_steps': [
                        'Visit the home page: /',
                        'Login as SuperAdmin (username: SuperAdmin, password: Password123)',
                        'Change the default password immediately'
                    ]
                }), 200
            else:
                return jsonify({
                    'status': 'error',
                    'message': message
                }), 500
                
    except Exception as e:
        logger.error(f"Init-db route error: {e}")
        import traceback
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc()
        }), 500

@app.route('/init-all-tables')
def init_all_tables():
    """
    Force create ALL tables (alias for init-db for backward compatibility)
    """
    return init_database_route()

@app.route('/init-notification-tables')
def init_notification_tables():
    """
    Specifically initialize notification tables
    Useful if only notification tables are missing
    """
    try:
        with app.app_context():
            from models import Notification, NotificationRecipient, NotificationPreference
            
            logger.info("Creating notification tables...")
            
            # Force create notification tables
            Notification.__table__.create(db.engine, checkfirst=True)
            logger.info("✓ notifications table created/verified")
            
            NotificationRecipient.__table__.create(db.engine, checkfirst=True)
            logger.info("✓ notification_recipients table created/verified")
            
            NotificationPreference.__table__.create(db.engine, checkfirst=True)
            logger.info("✓ notification_preferences table created/verified")
            
            return jsonify({
                'status': 'success',
                'message': 'Notification tables created successfully',
                'tables': [
                    'notifications',
                    'notification_recipients',
                    'notification_preferences'
                ]
            }), 200
            
    except Exception as e:
        logger.error(f"Notification table creation error: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/check-db')
def check_database():
    """
    Check database status and list all tables
    Useful for debugging
    """
    try:
        with app.app_context():
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            
            # Check for critical tables
            critical_tables = [
                'user', 'admin', 'student_profile',
                'notifications', 'notification_recipients',
                'course', 'assignment', 'quiz', 'exam'
            ]
            
            missing_tables = [t for t in critical_tables if t not in tables]
            
            # Check for SuperAdmin
            from models import Admin
            superadmin_exists = Admin.query.filter_by(username='SuperAdmin').first() is not None
            
            return jsonify({
                'status': 'ok',
                'total_tables': len(tables),
                'tables': sorted(tables),
                'critical_tables_status': {
                    'missing': missing_tables,
                    'all_present': len(missing_tables) == 0
                },
                'superadmin_exists': superadmin_exists,
                'database_url_set': bool(os.environ.get('DATABASE_URL')),
                'recommendation': 'Run /init-db to initialize database' if missing_tables else 'Database looks good!'
            }), 200
            
    except Exception as e:
        logger.error(f"Database check failed: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e),
            'recommendation': 'Database connection failed. Check DATABASE_URL environment variable.'
        }), 500

# ===== Blueprints =====
logger.info("📦 Registering blueprints...")

from admin_routes import admin_bp
from student_routes import student_bp
from teacher_routes import teacher_bp
from exam_routes import exam_bp
from vclass_routes import vclass_bp
from chat_routes import chat_bp
from finance_routes import finance_bp
from student_results_routes import results_bp
from student_transcript_routes import create_student_transcript_blueprint
from admissions.routes import admissions_bp

app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(student_bp, url_prefix='/student')
app.register_blueprint(teacher_bp, url_prefix='/teacher')
app.register_blueprint(exam_bp, url_prefix='/exam')
app.register_blueprint(vclass_bp, url_prefix='/vclass')
app.register_blueprint(chat_bp, url_prefix='/chat')
app.register_blueprint(finance_bp, url_prefix='/finance')
app.register_blueprint(results_bp, url_prefix='/student-results')
app.register_blueprint(create_student_transcript_blueprint())
app.register_blueprint(admissions_bp, url_prefix='/admissions')

logger.info("✅ All blueprints registered successfully")

# ===== Static Files =====
@app.route('/static/<path:filename>')
def static_files(filename):
    """Serve static files with proper headers"""
    try:
        return send_from_directory('static', filename)
    except Exception as e:
        logger.error(f"Static file error: {e}")
        abort(404)

# ===== Error Handlers =====
@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    logger.error(f"Internal server error: {error}")
    db.session.rollback()  # Rollback any failed database transactions
    return render_template('500.html'), 500

@app.errorhandler(Exception)
def handle_exception(e):
    """Handle all unhandled exceptions"""
    logger.exception("Unhandled exception: %s", e)
    db.session.rollback()
    
    if IS_PRODUCTION:
        return render_template('500.html'), 500
    else:
        # In development, show full error
        raise e

# ===== Debug Routes (Development Only) =====
@app.route('/debug/routes')
def debug_routes():
    """List all registered routes (debug only)"""
    if IS_PRODUCTION:
        abort(404)
    
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            'endpoint': rule.endpoint,
            'methods': ', '.join(sorted(rule.methods - {'HEAD', 'OPTIONS'})),
            'path': str(rule)
        })
    
    routes_html = "<h1>Registered Routes</h1><table border='1'><tr><th>Path</th><th>Methods</th><th>Endpoint</th></tr>"
    for route in sorted(routes, key=lambda x: x['path']):
        routes_html += f"<tr><td>{route['path']}</td><td>{route['methods']}</td><td>{route['endpoint']}</td></tr>"
    routes_html += "</table>"
    
    return routes_html

@app.route('/debug/config')
def debug_config():
    """Show current configuration (development only)"""
    if IS_PRODUCTION:
        abort(404)
    
    config_items = {
        'IS_PRODUCTION': IS_PRODUCTION,
        'FLASK_ENV': os.environ.get('FLASK_ENV', 'not set'),
        'RENDER': os.environ.get('RENDER', 'not set'),
        'DATABASE_URL': 'set' if os.environ.get('DATABASE_URL') else 'not set',
        'SECRET_KEY': 'set' if app.config.get('SECRET_KEY') else 'not set',
        'SOCKETIO_ASYNC_MODE': SOCKETIO_ASYNC_MODE
    }
    
    config_html = "<h1>Configuration</h1><table border='1'><tr><th>Key</th><th>Value</th></tr>"
    for key, value in config_items.items():
        config_html += f"<tr><td>{key}</td><td>{value}</td></tr>"
    config_html += "</table>"
    
    return config_html

# ===== Application Startup =====
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    
    logger.info("=" * 60)
    logger.info("STARTING LMS APPLICATION")
    logger.info("=" * 60)
    logger.info(f"Environment: {'PRODUCTION' if IS_PRODUCTION else 'DEVELOPMENT'}")
    logger.info(f"SocketIO mode: {SOCKETIO_ASYNC_MODE}")
    logger.info(f"Host: {'0.0.0.0' if IS_PRODUCTION else '127.0.0.1'}")
    logger.info(f"Port: {port}")
    logger.info("=" * 60)
    
    # Run the application
    socketio.run(
        app,
        host="0.0.0.0" if IS_PRODUCTION else "127.0.0.1",
        port=port,
        debug=not IS_PRODUCTION,
        allow_unsafe_werkzeug=not IS_PRODUCTION
    )

