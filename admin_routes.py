import logging

from flask import Blueprint, app, current_app, render_template, abort, request, redirect, url_for, flash, jsonify, session, send_from_directory

from flask_login import login_required, current_user, login_user

from werkzeug.security import generate_password_hash

from werkzeug.utils import secure_filename

from admissions.models import AdmissionVoucher, Application

from admissions.forms import CERTIFICATE_PROGRAMMES, DIPLOMA_PROGRAMMES, STUDY_FORMATS

from models import PasswordResetRequest, PasswordResetToken, StudentFeeBalance, db, User, Admin, StudentProfile, Quiz, Question, Option, StudentQuizSubmission, Assignment, CourseMaterial, Course, CourseLimit, TimetableEntry, TeacherProfile, AcademicCalendar, AcademicYear, ProgrammeFeeStructure, StudentFeeTransaction, Exam, ExamSubmission, ExamQuestion, ExamAttempt, ExamOption, ExamSet, ExamSetQuestion, FeePercentageSettings

from datetime import date, datetime, timedelta, time

from sqlalchemy import extract, asc, desc

from utils.email import send_teacher_registration_email, send_admin_registration_email

from sqlalchemy.orm import joinedload

from sqlalchemy.exc import IntegrityError

import os, json, csv, re, string, random

from sqlalchemy import func

from forms import AdminLoginForm, QuizForm, AdminRegisterForm, AssignmentForm, MaterialForm, CourseForm, CourseLimitForm, ExamForm, ExamSetForm, ExamQuestionForm

from admissions.forms import CERTIFICATE_PROGRAMMES, DIPLOMA_PROGRAMMES, STUDY_FORMATS

from services.grading_calculation_engine import GradingCalculationEngine

from services.semester_grading_service import SemesterGradingService

from utils.promotion import promote_student

from utils.backup import generate_quiz_csv_backup, backup_students_to_csv

from utils.helpers import get_programme_choices, get_level_choices, get_course_choices

from utils.serializers import (serialize_admin, serialize_submission, serialize_user, serialize_student, serialize_quiz, serialize_question, serialize_option, serialize_submission)

from utils.receipts import generate_receipt

from utils.index_generator import generate_index_number

from utils.email import send_approval_credentials_email, send_email, send_temporary_password_email, send_password_reset_email, send_continuing_student_credentials_email

from utils.notifications import create_assignment_notification, create_fee_notification

from utils.notification_engine import notify_quiz_created, notify_exam_scheduled, notify_fee_assigned

import uuid, secrets

from zipfile import ZipFile

import tempfile



admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

logger = logging.getLogger(__name__)



UPLOAD_FOLDER = 'static/uploads/quizzes'

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc', 'txt'}



def allowed_file(filename):

    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS



def is_admin_or_teacher():

    return getattr(current_user, 'role', None) in ['admin', 'teacher']


def is_superadmin_or_academic_admin():
    """Check if user is superadmin or academic admin (backup control for teacher features)"""
    if not isinstance(current_user, Admin):
        return False
    return current_user.is_superadmin or current_user.is_academic_admin


def check_admin_access():
    """Helper to check if current user is an admin; aborts if not"""
    if not isinstance(current_user, Admin):
        abort(403)





def ensure_release_columns():

    """Ensure new columns exist on semester_result_release table (compatible with PostgreSQL)."""

    try:
        from sqlalchemy import inspect, text
        from models import SemesterResultRelease

        SemesterResultRelease.__table__.create(db.engine, checkfirst=True)
        expected_columns = {
            'academic_year': 'VARCHAR(20)',
            'semester': 'VARCHAR(10)',
            'is_released': 'BOOLEAN DEFAULT FALSE',
            'is_locked': 'BOOLEAN DEFAULT FALSE',
            'released_at': 'TIMESTAMP',
            'locked_at': 'TIMESTAMP',
            'submitted_by': 'INTEGER',
            'submitted_by_name': 'TEXT',
            'submitted_at': 'TIMESTAMP',
            'submitted_note': 'TEXT',
            'submitted_courses': 'TEXT',
            'created_at': 'TIMESTAMP',
            'updated_at': 'TIMESTAMP',
        }

        with db.engine.begin() as connection:
            columns = {
                column['name']
                for column in inspect(connection).get_columns('semester_result_release')
            }
            for column_name, column_type in expected_columns.items():
                if column_name not in columns:
                    connection.execute(text(
                        f'ALTER TABLE semester_result_release '
                        f'ADD COLUMN {column_name} {column_type}'
                    ))
    except Exception:
        logger.exception("Failed ensuring semester_result_release columns")


def ensure_promotion_tables():
    """Ensure promotion tables exist before promotion pages query them."""
    from sqlalchemy import inspect, text
    from models import StudentPromotion

    StudentPromotion.__table__.create(db.engine, checkfirst=True)
    expected_columns = {
        'student_id': 'VARCHAR(50)',
        'promoted_by': 'INTEGER',
        'from_level': 'VARCHAR(10)',
        'to_level': 'VARCHAR(10)',
        'gpa': 'DOUBLE PRECISION',
        'academic_status': 'VARCHAR(50)',
        'academic_year': 'VARCHAR(10)',
        'promoted_at': 'TIMESTAMP',
        'created_at': 'TIMESTAMP',
        'notes': 'TEXT',
    }

    with db.engine.begin() as connection:
        columns = {
            column['name']
            for column in inspect(connection).get_columns('student_promotion')
        }
        for column_name, column_type in expected_columns.items():
            if column_name not in columns:
                connection.execute(text(
                    f'ALTER TABLE student_promotion '
                    f'ADD COLUMN {column_name} {column_type}'
                ))



@admin_bp.route('/login', methods=['GET', 'POST'])

def admin_login():

    form = AdminLoginForm()

    next_page = request.args.get('next')



    if form.validate_on_submit():

        username = form.username.data.strip()

        admin_id = form.user_id.data.strip()

        password = form.password.data.strip()



        # Query for admin or superadmin

        admin = Admin.query.filter_by(admin_id=admin_id).first()



        if admin and admin.username.lower() == username.lower() and admin.check_password(password):

            login_user(admin)

            role_text = "SuperAdmin" if admin.is_superadmin else "Admin"

            flash(f"Welcome back, {role_text} {admin.username}!", "success")

            

            # Redirect superadmin to superadmin dashboard, others to regular dashboard

            if admin.is_superadmin:

                return redirect(next_page or url_for("admin.superadmin_dashboard"))

            else:

                return redirect(next_page or url_for("admin.dashboard"))



        flash("Invalid admin login credentials.", "danger")

        return render_template("admin/login.html", form=form), 401  



    return render_template("admin/login.html", form=form)



"""

SMART ADMIN DASHBOARD ROUTER

Detects admin role and redirects to the appropriate dashboard

"""



from functools import wraps





# ============================================================

# PERMISSION DECORATORS

# ============================================================



def require_admin(f):

    """Require user to be any type of admin"""

    @wraps(f)

    def decorated(*args, **kwargs):

        if not isinstance(current_user, Admin):

            abort(403)

        return f(*args, **kwargs)

    return decorated





def require_admin_permission(permission_name):

    """Require admin to have specific permission"""

    def decorator(f):

        @wraps(f)

        def decorated(*args, **kwargs):

            if not isinstance(current_user, Admin):

                abort(403)

            

            if not current_user.has_permission(permission_name):

                abort(403)

            

            return f(*args, **kwargs)

        return decorated

    return decorator





def require_superadmin(f):

    """Require user to be superadmin"""

    @wraps(f)

    def decorated(*args, **kwargs):

        if not isinstance(current_user, Admin) or not current_user.is_superadmin:

            abort(403)

        return f(*args, **kwargs)

    return decorated





def require_finance_admin(f):

    """Require user to be finance admin or superadmin"""

    @wraps(f)

    def decorated(*args, **kwargs):

        if not isinstance(current_user, Admin):

            abort(403)

        if not (current_user.is_finance_admin or current_user.is_superadmin):

            abort(403)

        return f(*args, **kwargs)

    return decorated





def require_academic_admin(f):

    """Require user to be academic admin or superadmin"""

    @wraps(f)

    def decorated(*args, **kwargs):

        if not isinstance(current_user, Admin):

            abort(403)

        if not (current_user.is_academic_admin or current_user.is_superadmin):

            abort(403)

        return f(*args, **kwargs)

    return decorated





def require_admissions_admin(f):

    """Require user to be admissions admin or superadmin"""

    @wraps(f)

    def decorated(*args, **kwargs):

        if not isinstance(current_user, Admin):

            abort(403)

        if not (current_user.is_admissions_admin or current_user.is_superadmin):

            abort(403)

        return f(*args, **kwargs)

    return decorated





# ============================================================

# SMART DASHBOARD ROUTER

# ============================================================



@admin_bp.route('/dashboard')

@login_required

@require_admin

def dashboard():

    """

    Smart dashboard router - redirects to role-specific dashboard

    

    Logic:

    1. If Superadmin → Show general admin dashboard with all stats

    2. If Finance Admin → Redirect to finance dashboard

    3. If Academic Admin → Redirect to academic dashboard

    4. If Admissions Admin → Redirect to admissions dashboard

    

    If multiple roles, prioritize: Superadmin > Finance > Academic > Admissions

    """

    

    admin = current_user

    

    # Update last login

    try:

        admin.update_last_login()
    except Exception as e:
        logger.warning(f"Could not update last login: {e}")

    # ============================================================
    # ROUTE BY ROLE (with priority)
    # ============================================================

    if admin.is_superadmin:
        logger.info(f"SuperAdmin {admin.admin_id} accessing main dashboard")
        return redirect(url_for('admin.superadmin_dashboard'))

    elif admin.is_finance_admin:
        logger.info(f"Finance Admin {admin.admin_id} redirected to finance dashboard")
        return redirect(url_for('admin.finance_dashboard'))

    elif admin.is_academic_admin:
        logger.info(f"Academic Admin {admin.admin_id} redirected to academic dashboard")
        return redirect(url_for('admin.academic_dashboard'))

    elif admin.is_admissions_admin:
        logger.info(f"Admissions Admin {admin.admin_id} redirected to admissions dashboard")
        return redirect(url_for('admin.admissions_dashboard'))

    

    # No recognized role - show error

    else:

        logger.warning(f"Admin {admin.admin_id} has no recognized role: {admin.role}")

        abort(403)





# ============================================================

# SUPERADMIN DASHBOARD (General/Main)

# ============================================================



@admin_bp.route('/dashboard/superadmin')

@login_required

@require_superadmin

def superadmin_dashboard():

    """

    SuperAdmin comprehensive dashboard

    Shows statistics for all areas and admin management options

    """

    

    from models import User, Admin, StudentProfile

    from datetime import datetime

    

    admin = current_user

    

    try:

        # System-wide statistics

        student_count = User.query.filter_by(role='student').count()

        teacher_count = User.query.filter_by(role='teacher').count()

        total_admin_count = Admin.query.count()

        

        # Admin breakdown

        superadmin_count = Admin.query.filter_by(is_superadmin=True).count()

        finance_admin_count = Admin.query.filter_by(role='finance_admin').count()

        academic_admin_count = Admin.query.filter_by(role='academic_admin').count()

        admissions_admin_count = Admin.query.filter_by(role='admissions_admin').count()

        

        # Recent records

        recent_users = User.query.order_by(User.id.desc()).limit(10).all()

        recent_admins = Admin.query.order_by(Admin.created_at.desc()).limit(5).all()

        

        # Financial summary

        from models import StudentFeeBalance

        outstanding_balance = db.session.query(

            db.func.sum(StudentFeeBalance.amount_due - StudentFeeBalance.amount_paid)

        ).filter((StudentFeeBalance.amount_due - StudentFeeBalance.amount_paid) > 0).scalar() or 0

        

        context = {

            'admin': admin,

            'current_user': admin,

            'admin_role_display': admin.role_display,

            'is_superadmin': admin.is_superadmin,

            

            # System stats

            'student_count': student_count,

            'teacher_count': teacher_count,

            'total_admin_count': total_admin_count,

            

            # Admin breakdown

            'superadmin_count': superadmin_count,

            'finance_admin_count': finance_admin_count,

            'academic_admin_count': academic_admin_count,

            'admissions_admin_count': admissions_admin_count,

            

            # Recent

            'recent_users': recent_users,

            'recent_admins': recent_admins,

            

            # Permissions

            'accessible_sections': admin.get_accessible_sections(),

            'permissions': admin.get_permissions_summary(),

            

            # Finance

            'outstanding_balance': float(outstanding_balance),

        }

        

        return render_template('admin/admin_dashboard.html', **context)

    

    except Exception as e:

        logger.exception(f"Error loading superadmin dashboard: {e}")

        return render_template('admin/admin_dashboard.html', admin=admin), 500





# ============================================================

# FINANCE ADMIN DASHBOARD

# ============================================================



@admin_bp.route('/dashboard/finance')

@login_required

@require_finance_admin

def finance_dashboard():

    """

    Finance Admin dashboard

    Shows payments, fees, and financial data

    """

    

    from models import StudentFeeBalance, Admin

    

    admin = current_user

    

    if not admin.is_finance_admin and not admin.is_superadmin:

        abort(403)

    

    try:

        # Financial summary

        total_revenue = db.session.query(

            db.func.sum(StudentFeeTransaction.amount)

        ).filter(StudentFeeTransaction.is_approved == True).scalar() or 0

        

        outstanding_balance = db.session.query(

            db.func.sum(StudentFeeBalance.amount_due - StudentFeeBalance.amount_paid)

        ).filter((StudentFeeBalance.amount_due - StudentFeeBalance.amount_paid) > 0).scalar() or 0

        

        pending_count = StudentFeeTransaction.query.filter_by(is_approved=False).count()

        

        students_with_debt = db.session.query(

            db.func.count(db.distinct(StudentFeeBalance.student_id))

        ).filter((StudentFeeBalance.amount_due - StudentFeeBalance.amount_paid) > 0).scalar() or 0

        

        # Recent payments

        recent_payments = StudentFeeTransaction.query.order_by(

            StudentFeeTransaction.timestamp.desc()

        ).limit(20).all()

        

        context = {

            'admin': admin,

            'admin_role_display': admin.role_display,

            'total_revenue': float(total_revenue),

            'outstanding_balance': float(outstanding_balance),

            'pending_count': pending_count,

            'students_with_debt': students_with_debt,

            'recent_payments': recent_payments,

        }

        

        return render_template('admin/finance_admin_dashboard.html', **context)

    

    except Exception as e:

        logger.exception(f"Error loading finance dashboard: {e}")

        abort(500)





# ============================================================

# ACADEMIC ADMIN DASHBOARD

# ============================================================



@admin_bp.route('/dashboard/academic')

@login_required

@require_academic_admin

def academic_dashboard():

    """

    Academic Admin dashboard

    Shows grades, results, and academic data

    """

    

    admin = current_user

    

    if not admin.is_academic_admin and not admin.is_superadmin:

        abort(403)

    

    try:

        # Academic statistics

        from models import StudentProfile, AcademicYear, Course

        from datetime import date

        

        student_count = StudentProfile.query.count()

        course_count = Course.query.count()

        

        # Current academic year (based on today's date)

        today = date.today()

        current_year = AcademicYear.query.filter(

            AcademicYear.start_date <= today,

            AcademicYear.end_date >= today

        ).first()

        

        context = {

            'admin': admin,

            'admin_role_display': admin.role_display,

            'student_count': student_count,

            'course_count': course_count,

            'current_academic_year': current_year,

        }

        

        return render_template('admin/academic_dashboard.html', **context)

    

    except Exception as e:

        logger.exception(f"Error loading academic dashboard: {e}")

        abort(500)





# ============================================================

# ADMISSIONS ADMIN DASHBOARD

# ============================================================



@admin_bp.route('/dashboard/admissions')

@login_required

@require_admissions_admin

def admissions_dashboard():

    """

    Admissions Admin dashboard

    Shows applications and admissions data

    """

    

    admin = current_user

    

    if not admin.is_admissions_admin and not admin.is_superadmin:

        abort(403)

    

    try:

        from admissions.models import Application

        

        # Application statistics

        total_applications = Application.query.count()

        draft_count = Application.query.filter_by(status='draft').count()

        submitted_count = Application.query.filter_by(status='submitted').count()

        approved_count = Application.query.filter_by(status='approved').count()

        rejected_count = Application.query.filter_by(status='rejected').count()

        

        # Recent applications

        recent_applications = Application.query.order_by(

            Application.submitted_at.desc()

        ).limit(10).all()

        

        context = {

            'admin': admin,

            'admin_role_display': admin.role_display,

            'stats': {

                'total': total_applications,

                'draft': draft_count,

                'submitted': submitted_count,

                'approved': approved_count,

                'rejected': rejected_count,

            },

            'recent_applications': recent_applications,

        }

        

        return render_template('admin/manage_admissions.html', **context)

    

    except Exception as e:

        logger.exception(f"Error loading admissions dashboard: {e}")

        abort(500)





# ============================================================

# HELPER: Get dashboard URL for any admin

# ============================================================



def get_admin_dashboard_url(admin):

    """

    Get appropriate dashboard URL based on admin role

    

    Args:

        admin: Admin instance

    

    Returns:

        URL string

    """

    

    if admin.is_superadmin:

        return url_for('admin.superadmin_dashboard')

    elif admin.is_finance_admin:

        return url_for('admin.finance_dashboard')

    elif admin.is_academic_admin:

        return url_for('admin.academic_dashboard')

    elif admin.is_admissions_admin:

        return url_for('admin.admissions_dashboard')

    else:

        return url_for('admin.dashboard')


@admin_bp.route('/vetting/results')
@login_required
def result_vetting_list():
    if not (current_user.is_academic_admin or current_user.is_superadmin):
        abort(403)

    # Ensure DB schema has fields we expect (for older dev DBs)
    ensure_release_columns()

    # List semesters that have been submitted (locked) but not yet released
    submissions = SemesterResultRelease.query.filter_by(is_locked=True).order_by(SemesterResultRelease.locked_at.desc()).all()
    pending = [s for s in submissions if not s.is_released]

    return render_template('admin/result_vetting.html', submissions=pending)


@admin_bp.route('/past-releases')
@login_required
def past_releases():
    if not (current_user.is_academic_admin or current_user.is_superadmin):
        abort(403)

    # Ensure DB schema has fields we expect (for older dev DBs)
    ensure_release_columns()

    # List semesters that have been released
    releases = SemesterResultRelease.query.filter_by(is_released=True).order_by(SemesterResultRelease.released_at.desc()).all()

    return render_template('admin/past_releases.html', releases=releases)


@admin_bp.route('/vetting/results/<int:release_id>')

@login_required

def result_vetting_detail(release_id):

    if current_user.role not in ['superadmin', 'academic_admin']:

        abort(403)



    ensure_release_columns()



    release = SemesterResultRelease.query.get_or_404(release_id)



    # Attempt to parse submitted_courses JSON

    submitted_courses = []

    try:

        import json

        if release.submitted_courses:

            submitted_courses = json.loads(release.submitted_courses)

    except Exception as e:

        logger.warning(f"Failed to parse submitted_courses: {e}")

        submitted_courses = []



    logger.info(f"Submitted courses: {submitted_courses}")



    # Load detailed submissions for each submitted course

    course_details = []

    try:

        for c in submitted_courses:

            # Handle both dict and ID formats

            if isinstance(c, dict):

                cid = c.get('id')

            else:

                cid = c

            

            if not cid:

                logger.warning(f"No course ID found in: {c}")

                continue



            course_obj = Course.query.get(cid)

            if not course_obj:

                logger.warning(f"Course {cid} not found")

                continue



            logger.info(f"Processing course {cid}: {course_obj.name}")



            # Quizzes and their student submissions

            quiz_rows = (

                db.session.query(StudentQuizSubmission, Quiz, User)

                .join(Quiz, Quiz.id == StudentQuizSubmission.quiz_id)

                .join(User, User.id == StudentQuizSubmission.student_id)

                .filter(Quiz.course_id == cid)

                .order_by(Quiz.id, User.first_name, StudentQuizSubmission.submitted_at.desc())

                .all()

            )

            quizzes = []

            for sub, quiz, user in quiz_rows:

                quizzes.append({

                    'quiz_id': getattr(quiz, 'id', None),

                    'quiz_title': getattr(quiz, 'title', None),

                    'student_id': getattr(user, 'user_id', None),

                    'student_name': f"{user.first_name} {user.last_name}".strip(),

                    'score': float(getattr(sub, 'score', 0) or 0),

                    'max_score': float(getattr(quiz, 'max_score', 0) or 0),

                    'submitted_at': getattr(sub, 'submitted_at', None),

                    'feedback': getattr(sub, 'feedback', None)

                })



            logger.info(f"Found {len(quizzes)} quiz submissions for course {cid}")



            # Assignments

            assignment_rows = (

                db.session.query(AssignmentSubmission, Assignment, User)

                .join(Assignment, Assignment.id == AssignmentSubmission.assignment_id)

                .join(User, User.id == AssignmentSubmission.student_id)

                .filter(Assignment.course_id == cid)

                .order_by(Assignment.id, User.first_name, AssignmentSubmission.submitted_at.desc())

                .all()

            )

            assignments = []

            for sub, assignment, user in assignment_rows:

                assignments.append({

                    'assignment_id': getattr(assignment, 'id', None),

                    'assignment_title': getattr(assignment, 'title', None),

                    'student_id': getattr(user, 'user_id', None),

                    'student_name': f"{user.first_name} {user.last_name}".strip(),

                    'score': float(getattr(sub, 'score', 0) or 0),

                    'max_score': float(getattr(assignment, 'max_score', 0) or 0),

                    'submitted_at': getattr(sub, 'submitted_at', None),

                    'feedback': getattr(sub, 'feedback', None)

                })



            logger.info(f"Found {len(assignments)} assignment submissions for course {cid}")



            # Exams

            exam_rows = (

                db.session.query(ExamSubmission, Exam, User)

                .join(Exam, Exam.id == ExamSubmission.exam_id)

                .join(User, User.id == ExamSubmission.student_id)

                .filter(Exam.course_id == cid)

                .order_by(Exam.id, User.first_name, ExamSubmission.submitted_at.desc())

                .all()

            )

            exams = []

            for sub, exam, user in exam_rows:

                exams.append({

                    'exam_id': getattr(exam, 'id', None),

                    'exam_title': getattr(exam, 'title', None),

                    'student_id': getattr(user, 'user_id', None),

                    'student_name': f"{user.first_name} {user.last_name}".strip(),

                    'score': float(getattr(sub, 'score', 0) or 0),

                    'max_score': float(getattr(exam, 'max_score', 0) or 0),

                    'submitted_at': getattr(sub, 'submitted_at', None),

                    'feedback': getattr(sub, 'feedback', None)

                })



            logger.info(f"Found {len(exams)} exam submissions for course {cid}")



            course_details.append({

                'course': {

                    'id': course_obj.id,

                    'code': getattr(course_obj, 'code', ''),

                    'name': getattr(course_obj, 'name', '')

                },

                'quizzes': quizzes,

                'assignments': assignments,

                'exams': exams

            })



    except Exception as e:

        logger.exception(f'Failed loading course submission details: {e}')



    logger.info(f"Total courses with details: {len(course_details)}")



    return render_template(

        'admin/result_vetting_detail.html',

        release=release,

        submitted_courses=submitted_courses,

        course_details=course_details

    )



@admin_bp.route('/results/approve', methods=['POST'])

@login_required

def api_approve_results():

    # Allow both admin and superadmin roles to approve/release results

    if getattr(current_user, 'role', None) not in ['admin', 'superadmin']:

        return jsonify({'error': 'Forbidden'}), 403



    data = request.get_json() or {}

    academic_year = data.get('academic_year')

    semester = data.get('semester')



    if not academic_year or not semester:

        return jsonify({'error': 'academic_year and semester required'}), 400



    # Attempt to finalize all course grades for the semester first

    finalize_result = SemesterGradingService.finalize_all_course_grades(academic_year, semester)



    # If there were errors finalizing, include details but continue to attempt release

    if finalize_result.get('total_errors', 0) > 0:

        # try release anyway; release_semester_results will refuse if unfinalized remain

        release_result = SemesterGradingService.release_semester_results(academic_year, semester)

        return jsonify({

            'success': release_result.get('success', False),

            'message': release_result.get('message', 'Release attempted'),

            'finalize': finalize_result

        }), (200 if release_result.get('success') else 400)



    # No finalize errors — proceed to release

    release_result = SemesterGradingService.release_semester_results(academic_year, semester)

    if release_result.get('success'):

        return jsonify({'success': True, 'message': release_result.get('message', 'Released'), 'finalize': finalize_result})

    return jsonify({'success': False, 'message': release_result.get('message', 'Failed'), 'finalize': finalize_result}), 400





@admin_bp.route('/results/reject', methods=['POST'])

@login_required

def api_reject_results():

    # Allow both admin and superadmin roles to reject/unlock results

    if getattr(current_user, 'role', None) not in ['admin', 'superadmin']:

        return jsonify({'error': 'Forbidden'}), 403



    data = request.get_json() or {}

    academic_year = data.get('academic_year')

    semester = data.get('semester')



    if not academic_year or not semester:

        return jsonify({'error': 'academic_year and semester required'}), 400



    result = SemesterGradingService.unlock_semester(academic_year, semester)

    if result.get('success'):

        return jsonify({'success': True, 'message': result.get('message', 'Unlocked')})

    return jsonify({'success': False, 'message': result.get('message', 'Failed')}), 400



# --------------- Continuing Student Registration (Level 200-400) ---------------

@admin_bp.route('/register/continuing-student', methods=['GET', 'POST'])

@login_required

def register_continuing_student():

    """

    Register continuing students (Level 200-400)

    For students already part of the school who are advancing to higher levels

    

    Restrictions:

    - Only allows Admissions Admin or SuperAdmin

    - Only creates students (role='student')

    - Only allows levels 200, 300, or 400

    - Creates both User record and StudentProfile record

    """

    

    # Check permissions

    if not isinstance(current_user, Admin):

        abort(403)

    

    if not (current_user.is_admissions_admin or current_user.is_superadmin):

        flash("❌ Only Admissions Admin or SuperAdmin can register continuing students.", 'danger')

        abort(403)



    # Combine certificate and diploma programmes (remove blank option from both)

    cert_progs = [p for p in CERTIFICATE_PROGRAMMES if p[0] != '']

    dip_progs = [p for p in DIPLOMA_PROGRAMMES if p[0] != '']

    programmes = cert_progs + dip_progs

    study_formats = [(s[0], s[1]) for s in STUDY_FORMATS if s[0] != '']

    

    if request.method == 'POST':

        # ========================================================

        # FORM VALIDATION

        # ========================================================

        first_name = request.form.get('first_name', '').strip()

        last_name = request.form.get('last_name', '').strip()

        middle_name = request.form.get('middle_name', '').strip()

        email = (request.form.get('email') or '').strip() or None

        temp_password = request.form.get('password', '').strip()

        

        programme = request.form.get('current_programme', '').strip()

        level_str = request.form.get('programme_level', '').strip()

        study_format = request.form.get('study_format', 'Regular').strip()

        academic_year = request.form.get('academic_year', '').strip()

        semester = request.form.get('semester', '').strip()



        if not (first_name and last_name and programme and level_str and temp_password):

            flash("❌ First name, last name, programme, level, and password are required.", 'danger')

            return render_template(

                'admin/register_continuing_student.html',

                programmes=programmes,

                study_formats=study_formats,

                form=AdminRegisterForm()

            )



        # Validate level is 200, 300, or 400

        try:

            programme_level = int(level_str)

            if programme_level not in [200, 300, 400]:

                flash("❌ Continuing students must be Level 200, 300, or 400.", 'danger')

                return render_template(

                    'admin/register_continuing_student.html',

                    programmes=programmes,

                    study_formats=study_formats,

                    form=AdminRegisterForm()

                )

        except (ValueError, TypeError):

            flash("❌ Invalid level format.", 'danger')

            return render_template(

                'admin/register_continuing_student.html',

                programmes=programmes,

                study_formats=study_formats,

                form=AdminRegisterForm()

            )



        # ========================================================

        # PROFILE PICTURE HANDLING

        # ========================================================

        picture = request.files.get('profile_picture')

        profile_picture = "default_avatar.png"



        if picture and picture.filename:

            filename = secure_filename(picture.filename)

            unique_filename = f"{uuid.uuid4().hex}_{filename}"

            picture_path = os.path.join(current_app.config['PROFILE_PICS_FOLDER'], unique_filename)

            os.makedirs(current_app.config['PROFILE_PICS_FOLDER'], exist_ok=True)

            picture.save(picture_path)

            profile_picture = unique_filename



        # ========================================================

        # EMAIL DUPLICATE CHECK

        # ========================================================

        if email:

            existing_email = User.query.filter_by(email=email).first()

            if existing_email:

                flash("❌ That email is already in use by another account.", "danger")

                return render_template(

                    'admin/register_continuing_student.html',

                    programmes=programmes,

                    study_formats=study_formats,

                    form=AdminRegisterForm()

                )



        # ========================================================

        # GENERATE USERNAME & USER_ID

        # ========================================================

        username = request.form.get('username')

        if not username:

            username = generate_unique_username(first_name, middle_name, last_name, 'student')



        if User.query.filter_by(username=username).first():

            flash("❌ Username already exists. Please provide a unique username.", 'danger')

            return render_template(

                'admin/register_continuing_student.html',

                programmes=programmes,

                study_formats=study_formats,

                form=AdminRegisterForm()

            )



        # Generate unique student ID

        prefix = 'STD'

        count = User.query.filter_by(role='student').count() + 1

        while User.query.filter_by(user_id=f"{prefix}{count:03d}").first():

            count += 1

        user_id = f"{prefix}{count:03d}"



        # ========================================================

        # CREATE USER & STUDENT PROFILE

        # ========================================================

        try:

            # Create User record

            new_user = User(

                user_id=user_id,

                username=username,

                email=email,

                first_name=first_name,

                middle_name=middle_name,

                last_name=last_name,

                role='student',

                profile_picture=profile_picture

            )

            new_user.set_password(temp_password)

            db.session.add(new_user)

            db.session.flush()  # Flush to ensure user_id is available



            # Create StudentProfile record

            dob_str = request.form.get('dob', '').strip()

            dob = datetime.strptime(dob_str, '%Y-%m-%d') if dob_str else None

            

            index_number = request.form.get('index_number', '').strip()

            

            student_profile = StudentProfile(

                user_id=user_id,

                dob=dob,

                gender=request.form.get('gender', '').strip(),

                nationality=request.form.get('nationality', '').strip(),

                religion=request.form.get('religion', '').strip(),

                address=request.form.get('address', '').strip(),

                city=request.form.get('city', '').strip(),

                state=request.form.get('state', '').strip(),

                postal_code=request.form.get('postal_code', '').strip(),

                phone=request.form.get('phone', '').strip(),

                email=(request.form.get('email') or '').strip(),

                current_programme=programme,

                programme_level=programme_level,

                study_format=study_format,

                academic_year=academic_year,

                semester=semester,

                index_number=index_number,

                guardian_name=request.form.get('guardian_name', '').strip(),

                guardian_relation=request.form.get('guardian_relation', '').strip(),

                guardian_phone=request.form.get('guardian_phone', '').strip(),

                guardian_email=request.form.get('guardian_email', '').strip(),

                guardian_address=request.form.get('guardian_address', '').strip(),

                admission_date=datetime.now().date()

            )

            db.session.add(student_profile)

            db.session.commit()

            # Send credentials email to student
            if email:
                logger.info(f"Attempting to send credentials email to continuing student: {email}")
                try:
                    email_sent = send_continuing_student_credentials_email(
                        email=email,
                        first_name=first_name,
                        last_name=last_name,
                        username=username,
                        student_id=user_id,
                        index_number=index_number,
                        temp_password=temp_password,
                        programme=programme,
                        level=programme_level
                    )
                    if email_sent:
                        logger.info(f"✅ Successfully sent credentials email to {email}")
                        flash(f"📧 Credentials email sent to {email}", "info")
                    else:
                        logger.warning(f"⚠️ Failed to send credentials email to {email}")
                        flash("⚠️ Student registered but email failed to send. Please provide credentials manually.", "warning")
                except Exception as e:
                    logger.error(f"❌ Exception sending continuing student email to {email}: {e}")
                    flash("⚠️ Student registered but email failed to send. Please provide credentials manually.", "warning")
            else:
                logger.info(f"⚠️ No email address provided for student {user_id} - skipping email sending")
                flash("⚠️ Student registered without email address. No credentials email sent.", "warning")

            flash(f"✅ Continuing student '{first_name} {last_name}' registered successfully! Student ID: {user_id} | Username: {username} | Index: {index_number}", "success")

            return redirect(url_for('admin.dashboard'))


        except IntegrityError:

            db.session.rollback()

            flash("❌ Database integrity error (duplicate user/email/username). Please try again with different data.", "danger")

            return render_template(

                'admin/register_continuing_student.html',

                programmes=programmes,

                study_formats=study_formats,

                form=AdminRegisterForm()

            )



        except Exception as e:

            db.session.rollback()

            logger.exception(f"Error registering continuing student: {e}")

            flash(f"❌ Error saving continuing student: {str(e)}", "danger")

            return render_template(

                'admin/register_continuing_student.html',

                programmes=programmes,

                study_formats=study_formats,

                form=AdminRegisterForm()

            )



    # GET request - show form

    return render_template(

        'admin/register_continuing_student.html',

        programmes=programmes,

        study_formats=study_formats,

        form=AdminRegisterForm()

    )





@admin_bp.route('/generate-student-credentials', methods=['POST'])

@login_required

def generate_student_credentials():

    """

    API endpoint to auto-generate username and password for continuing student

    

    POST data:

    - first_name: First name

    - middle_name: Middle name (optional)

    - last_name: Last name

    

    Returns:

    {

        "success": true,

        "username": "jolamptey@st.vtiu.edu.gh",

        "password": "Abc123!@#"

    }

    """

    try:

        first_name = request.json.get('first_name', '').strip()

        middle_name = request.json.get('middle_name', '').strip()

        last_name = request.json.get('last_name', '').strip()

        

        if not (first_name and last_name):

            return jsonify({'success': False, 'error': 'First name and last name required'}), 400

        

        # Generate username using existing function

        username = generate_unique_username(first_name, middle_name, last_name, 'student')

        

        # Generate random password

        password = generate_random_password(length=10)

        

        return jsonify({

            'success': True,

            'username': username,

            'password': password

        })

    

    except Exception as e:

        logger.exception(f"Error generating credentials: {e}")

        return jsonify({'success': False, 'error': str(e)}), 500





@admin_bp.route('/generate-student-index', methods=['POST'])

@login_required

def generate_student_index():

    """

    API endpoint to auto-generate index number for continuing student

    

    POST data:

    - programme: Programme name (e.g., 'Midwifery')

    

    Returns:

    {

        "success": true,

        "index_number": "0926001"

    }

    """

    try:

        programme = request.json.get('programme', '').strip()

        

        if not programme:

            return jsonify({'success': False, 'error': 'Programme name required'}), 400

        

        # Generate index number using utility function

        index_number = generate_index_number(programme)

        

        return jsonify({

            'success': True,

            'index_number': index_number

        })

    

    except ValueError as e:

        logger.warning(f"Invalid programme for index generation: {e}")

        return jsonify({'success': False, 'error': str(e)}), 400

    except Exception as e:

        logger.exception(f"Error generating index number: {e}")

        return jsonify({'success': False, 'error': str(e)}), 500



# Non-route helper function

def get_new_employee_id():

    """Generate a unique Employee ID."""

    prefix = 'EMP'

    count = TeacherProfile.query.count() + 1

    while True:

        emp_id = f"{prefix}{count:03d}"  # e.g., EMP001

        if not TeacherProfile.query.filter_by(employee_id=emp_id).first():

            return emp_id

        count += 1



# Route to provide employee ID via AJAX

@admin_bp.route('/generate-employee-id')

@login_required

def generate_employee_id_route():

    prefix = 'EMP'

    count = TeacherProfile.query.count() + 1



    while True:

        emp_id = f"{prefix}{count:03d}"

        if not TeacherProfile.query.filter_by(employee_id=emp_id).first():

            break

        count += 1



    return jsonify({'employee_id': emp_id})



"""

ENHANCED ADMIN REGISTRATION ROUTE

Allows Superadmin to create other admins (Finance Admin, Academic Admin, etc.)

with granular permission control

"""






@admin_bp.route('/generate-admin-id')

@login_required

def generate_admin_id_route():

    role = request.args.get('role', '').strip().lower()



    prefix_map = {

        'finance_admin': 'FIN',

        'academic_admin': 'ACA',

        'admissions_admin': 'ADM',

        'superadmin': 'SAA'

    }



    prefix = prefix_map.get(role)

    if not prefix:

        return jsonify({'admin_id': ''})



    count = Admin.query.filter_by(role=role).count() + 1



    while True:

        admin_id = f"{prefix}{count:03d}"

        if not Admin.query.filter_by(admin_id=admin_id).first():

            break

        count += 1



    return jsonify({'admin_id': admin_id})



@admin_bp.route('/register', methods=['GET', 'POST'])

@login_required

def register_user():



    if not isinstance(current_user, Admin):

        abort(403)



    if not (current_user.is_superadmin or current_user.is_admissions_admin):

        abort(403)



    form = AdminRegisterForm()



    if current_user.is_superadmin:

        form.role.choices = [

            ('teacher', 'Teacher'),

            ('finance_admin', 'Finance Admin'),

            ('academic_admin', 'Academic Admin'),

            ('admissions_admin', 'Admissions Admin'),

            ('superadmin', 'Super Admin')

        ]

    else:

        form.role.choices = [('teacher', 'Teacher')]



    if request.method == 'GET':

        return render_template('admin/register_user.html', form=form, is_superadmin=current_user.is_superadmin)



    # ==========================================================

    # FORM DATA

    # ==========================================================

    role = request.form.get('role', '').strip().lower()

    first_name = request.form.get('first_name', '').strip()

    last_name = request.form.get('last_name', '').strip()

    middle_name = request.form.get('middle_name', '').strip()

    email = None

    temp_password = request.form.get('password', '').strip()



    if not (first_name and last_name and role and temp_password):

        flash("❌ Missing required fields.", 'danger')

        return redirect(url_for('admin.register_user'))



    if not current_user.is_superadmin and role != 'teacher':

        flash("❌ Only Superadmin can create Admins.", 'danger')

        return redirect(url_for('admin.register_user'))



    # ==========================================================

    # PROFILE PICTURE

    # ==========================================================

    picture = request.files.get('profile_picture')

    profile_picture = "default_avatar.png"



    if picture and picture.filename:

        filename = secure_filename(picture.filename)

        unique_filename = f"{uuid.uuid4().hex}_{filename}"

        path = os.path.join(current_app.config['PROFILE_PICS_FOLDER'], unique_filename)

        os.makedirs(current_app.config['PROFILE_PICS_FOLDER'], exist_ok=True)

        picture.save(path)

        profile_picture = unique_filename



    # ==========================================================

    # USERNAME

    # ==========================================================

    username = request.form.get('username') or generate_unique_username(first_name, middle_name, last_name, role)



    if User.query.filter_by(username=username).first() or Admin.query.filter_by(username=username).first():

        flash("❌ Username already exists.", 'danger')

        return redirect(url_for('admin.register_user'))



    # Generate the login email from the name and role. This is authoritative
    # so manually entered or reused addresses cannot create duplicate accounts.
    email = generate_unique_email(first_name, middle_name, last_name, role)



    try:



        # ======================================================

        # 🔵 TEACHER REGISTRATION (User table used)

        # ======================================================

        if role == 'teacher':



            # Generate user_id

            prefix = 'TCH'

            count = User.query.filter_by(role='teacher').count() + 1

            while User.query.filter_by(user_id=f"{prefix}{count:03d}").first():

                count += 1

            user_id = f"{prefix}{count:03d}"



            new_user = User(

                user_id=user_id,

                username=username,

                email=email,

                first_name=first_name,

                middle_name=middle_name,

                last_name=last_name,

                role='teacher',

                profile_picture=profile_picture

            )

            new_user.set_password(temp_password)

            db.session.add(new_user)

            db.session.flush()



            # Employee ID

            count = TeacherProfile.query.count() + 1

            while TeacherProfile.query.filter_by(employee_id=f"EMP{count:03d}").first():

                count += 1

            employee_id = f"EMP{count:03d}"



            teacher_profile = TeacherProfile(

                user_id=user_id,

                employee_id=employee_id,

                department=request.form.get('department')

            )

            db.session.add(teacher_profile)

            db.session.commit()

            # Send registration email with credentials
            try:
                email_sent = send_teacher_registration_email(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    username=username,
                    user_id=user_id,
                    employee_id=employee_id,
                    temp_password=temp_password
                )
                if email_sent:
                    flash(f"✅ Teacher registered! User ID: {user_id} | Employee ID: {employee_id} | Credentials sent to {email}", "success")
                else:
                    flash(f"✅ Teacher registered! User ID: {user_id} | Employee ID: {employee_id} | ⚠️ Email failed to send", "warning")
            except Exception as e:
                flash(f"✅ Teacher registered! User ID: {user_id} | Employee ID: {employee_id} | ⚠️ Email error: {str(e)}", "warning")

            return redirect(url_for('admin.dashboard'))



        # ======================================================

        # 🔴 ADMIN REGISTRATION (Admin table ONLY)

        # ======================================================

        else:



            prefix_map = {

                'finance_admin': 'FIN',

                'academic_admin': 'ACA',

                'admissions_admin': 'ADM',

                'superadmin': 'SAA'

            }



            prefix = prefix_map.get(role)

            count = Admin.query.filter_by(role=role).count() + 1

            while Admin.query.filter_by(admin_id=f"{prefix}{count:03d}").first():

                count += 1

            admin_id = f"{prefix}{count:03d}"



            new_admin = Admin(

                admin_id=admin_id,

                username=username,

                email=email or username,

                role=role,

                is_superadmin=(role == 'superadmin'),

                job_title=request.form.get('job_title'),

                department=request.form.get('department_admin'),

                phone=request.form.get('phone'),

                notes=request.form.get('notes'),

                profile_picture=profile_picture

            )

            new_admin.set_password(temp_password)



            # Apply permission preset automatically

            if role == 'finance_admin':

                Admin.apply_finance_admin_preset(new_admin)

            elif role == 'academic_admin':

                Admin.apply_academic_admin_preset(new_admin)

            elif role == 'admissions_admin':

                Admin.apply_admissions_admin_preset(new_admin)

            elif role == 'superadmin':

                Admin.apply_superadmin_preset(new_admin)



            db.session.add(new_admin)

            db.session.commit()

            # Send registration email with credentials
            try:
                email_sent = send_admin_registration_email(
                    email=email or username,
                    first_name=first_name,
                    last_name=last_name,
                    username=username,
                    admin_id=admin_id,
                    role=role,
                    temp_password=temp_password
                )
                if email_sent:
                    flash(f"✅ {new_admin.role_display} registered! Admin ID: {admin_id} | Credentials sent to {email or username}", "success")
                else:
                    flash(f"✅ {new_admin.role_display} registered! Admin ID: {admin_id} | ⚠️ Email failed to send", "warning")
            except Exception as e:
                flash(f"✅ {new_admin.role_display} registered! Admin ID: {admin_id} | ⚠️ Email error: {str(e)}", "warning")

            return redirect(url_for('admin.dashboard'))



    except Exception as e:

        db.session.rollback()

        flash(f"❌ Error: {str(e)}", "danger")

        return redirect(url_for('admin.register_user'))



# ============================================================

# HELPER ROUTE: Get available admin roles for the current user

# ============================================================

@admin_bp.route('/admin-roles', methods=['GET'])

@login_required

def get_available_admin_roles():

    """

    Return list of admin roles that the current user can create

    Superadmin can create all roles; regular admins cannot create any

    """

    

    if not getattr(current_user, 'role', None) == 'admin':

        return jsonify({'error': 'Forbidden'}), 403



    if getattr(current_user, 'is_superadmin', False):

        # Superadmin can create all roles

        roles = [

            {'value': 'teacher', 'label': 'Teacher'},

            {'value': 'finance_admin', 'label': '💰 Finance Admin'},

            {'value': 'academic_admin', 'label': '📚 Academic Admin'},

            {'value': 'admissions_admin', 'label': '👥 Admissions Admin'},

            {'value': 'superadmin', 'label': '⭐ Super Admin'}

        ]

    else:

        # Regular admin can only create teachers

        roles = [

            {'value': 'teacher', 'label': 'Teacher'}

        ]



    return jsonify({'roles': roles})





# ============================================================

# HELPER ROUTE: Get permissions for a specific admin role

# ============================================================

@admin_bp.route('/role-permissions/<role>', methods=['GET'])

@login_required

def get_role_permissions(role):

    """

    Get default permissions for a specific admin role

    Helps auto-populate permission checkboxes based on role selection

    """

    

    if not getattr(current_user, 'role', None) == 'admin':

        return jsonify({'error': 'Forbidden'}), 403



    # Define default permissions per role

    role_defaults = {

        'teacher': {

            'can_view_finances': False,

            'can_edit_finances': False,

            'can_view_academics': False,

            'can_edit_academics': False,

            'can_view_admissions': False,

            'can_edit_admissions': False,

            'can_manage_users': False,

            'can_view_reports': False,

            'can_export_data': False

        },

        'finance_admin': {

            'can_view_finances': True,

            'can_edit_finances': True,

            'can_view_academics': False,

            'can_edit_academics': False,

            'can_view_admissions': False,

            'can_edit_admissions': False,

            'can_manage_users': False,

            'can_view_reports': True,

            'can_export_data': True

        },

        'academic_admin': {

            'can_view_finances': False,

            'can_edit_finances': False,

            'can_view_academics': True,

            'can_edit_academics': True,

            'can_view_admissions': False,

            'can_edit_admissions': False,

            'can_manage_users': False,

            'can_view_reports': True,

            'can_export_data': True

        },

        'admissions_admin': {

            'can_view_finances': False,

            'can_edit_finances': False,

            'can_view_academics': True,

            'can_edit_academics': False,

            'can_view_admissions': True,

            'can_edit_admissions': True,

            'can_manage_users': False,

            'can_view_reports': True,

            'can_export_data': True

        },

        'superadmin': {

            'can_view_finances': True,

            'can_edit_finances': True,

            'can_view_academics': True,

            'can_edit_academics': True,

            'can_view_admissions': True,

            'can_edit_admissions': True,

            'can_manage_users': True,

            'can_view_reports': True,

            'can_export_data': True

        }

    }



    permissions = role_defaults.get(role, {})

    return jsonify({'permissions': permissions})



@admin_bp.route('/get-students-by-programme/<programme>/<level>')

@login_required

def get_students_by_programme_level(programme, level):

    """Get all students in a specific programme and level (tertiary version)"""

    if getattr(current_user, 'role', None) != 'admin':

        abort(403)



    try:

        level_int = int(level)

    except ValueError:

        return jsonify({"error": "Invalid level"}), 400



    students = (

        StudentProfile.query

        .join(User)

        .filter(

            StudentProfile.current_programme == programme,

            StudentProfile.programme_level == level_int

        )

        .order_by(User.first_name, User.last_name)

        .all()

    )



    return jsonify({

        "students": [

            {

                "id": s.id,

                "user_id": s.user_id,

                "name": s.user.full_name if hasattr(s, 'user') else f'Student #{s.id}',

                "index_number": s.index_number or "N/A",

                "programme": programme,

                "level": level_int

            }

            for s in students

        ]

    })



import re

from models import User



def clean(n):

    """Remove non-alphabetic characters and convert to lowercase"""

    return re.sub(r'[^a-zA-Z]', '', (n or '')).lower().strip()



def generate_unique_email(first_name, middle_name, last_name, role):
    """Generate a unique role-specific institutional email address."""
    first = clean(first_name)
    middle = clean(middle_name)
    last = clean(last_name)

    if not first and middle:
        first, middle = middle, ''
    if not first or not last:
        raise ValueError("First name and last name are required")

    local_part = first[0] + (middle[0] if middle else '') + last
    domain_map = {
        'student': 'st.vtiu.edu.gh',
        'teacher': 'tch.vtiu.edu.gh',
        'finance_admin': 'finance.vtiu.edu.gh',
        'academic_admin': 'academics.vtiu.edu.gh',
        'admissions_admin': 'admissions.vtiu.edu.gh',
        'superadmin': 'admin.vtiu.edu.gh',
    }
    domain = domain_map.get(role.lower(), 'vtiu.edu.gh')

    counter = 0
    while True:
        suffix = str(counter) if counter else ''
        email = f"{local_part}{suffix}@{domain}"
        if not User.query.filter_by(email=email).first() and not Admin.query.filter_by(email=email).first():
            return email
        counter += 1


def generate_unique_username(first_name, middle_name, last_name, role):

    """

    Generate unique username using initials + full last name

    

    Format: [First initial][Middle initial]LastName@domain

    

    Examples:

    - Joseph Odartei Lamptey → jolamptey@st.vtiu.edu.gh

    - John Smith → jsmith@st.vtiu.edu.gh

    - Mary Jane Watson → mjwatson@st.vtiu.edu.gh

    - "" Odartei Lamptey → olamptey@st.vtiu.edu.gh (shifts middle to first)

    

    Args:

        first_name: First name

        middle_name: Middle name(s)

        last_name: Last name (ALWAYS USED IN FULL)

        role: User role (student, teacher, finance_admin, etc.)

    

    Returns:

        Unique username in format: [initials]lastname@domain.edu.gh

    

    Raises:

        ValueError: If first_name or last_name missing

    """

    

    # Clean all name parts

    first = clean(first_name)

    middle = clean(middle_name)

    last = clean(last_name)

    

    # ✅ FIX: Handle name shifting if first name is empty

    if not first and middle:

        # If first name missing but middle exists → shift middle to first

        first = middle

        middle = ''

    

    # Validate we have minimum required names

    if not first or not last:

        raise ValueError("First name and last name are required")

    

    # ✅ BUILD USERNAME: [F initial][M initial]Last name (in full)

    # Start with first initial

    base = first[0]  # Just the initial of first name

    

    # Add middle initial if exists

    if middle:

        base += middle[0]  # Just the initial of middle name

    

    # Add FULL last name (not just initial)

    base += last  # Full last name, NOT just last[0]

    

    # ✅ DOMAIN MAPPING (each role gets its own domain)

    domain_map = {

        'student': 'st.vtiu.edu.gh',

        'teacher': 'tch.vtiu.edu.gh',

        'finance_admin': 'finance.vtiu.edu.gh',

        'academic_admin': 'academic.vtiu.edu.gh',

        'admissions_admin': 'admissions.vtiu.edu.gh',

        'superadmin': 'admin.vtiu.edu.gh',  # Only superadmin uses admin.vtiu.edu.gh

    }

    domain = domain_map.get(role.lower(), 'vtiu.edu.gh')

    

    # ✅ ENSURE UNIQUENESS with counter

    counter = 0

    while True:

        # First iteration: no suffix, just base@domain

        # Subsequent: base1@domain, base2@domain, etc.

        suffix = str(counter) if counter else ''

        username = f"{base}{suffix}@{domain}"

        

        # Check both User and Admin tables for uniqueness

        exists_in_user = User.query.filter_by(username=username).first()

        exists_in_admin = Admin.query.filter_by(username=username).first()

        

        if not exists_in_user and not exists_in_admin:

            return username

        

        counter += 1



def setup_username_password_routes(admin_bp):

    """Setup routes for username and password generation"""



    @admin_bp.route('/generate-username', methods=['POST'])

    def generate_username_api():

        """

        API endpoint to generate unique username

        

        POST body:

        {

            "first_name": "Joseph",

            "middle_name": "Odartei",

            "last_name": "Lamptey",

            "role": "student"

        }

        

        Returns:

        {

            "username": "jolamptey@st.vtiu.edu.gh",

            "success": true

        }

        """

        try:

            data = request.get_json()



            username = generate_unique_username(

                data.get('first_name', ''),

                data.get('middle_name', ''),

                data.get('last_name', ''),

                data.get('role', 'student')

            )

            

            return jsonify({

                'success': True,

                'username': username

            }), 200

        

        except ValueError as e:

            return jsonify({

                'success': False,

                'error': str(e)

            }), 400

        

        except Exception as e:

            return jsonify({

                'success': False,

                'error': 'Server error: ' + str(e)

            }), 500

    

def generate_random_password(length=8):

    """

    Generate a strong random password

    

    Args:

        length: Password length (default 12 for security)

    

    Returns:

        Random password with letters, digits, and special characters

    """

    chars = string.ascii_letters + string.digits + '!@#$%^&*()'

    return ''.join(random.choices(chars, k=length))



@admin_bp.route('/generate-passwords', methods=['GET'])

def generate_passwords():

    """

    API endpoint to generate random passwords

        

    Query params:

    - count: number of passwords to generate (default 3, max 10)

    - length: password length (default 12, range 8-20)

        

    Returns:

    {

        "success": true,

        "passwords": ["Abc123!@#$", "Xyz789!@#$", "Def456!@#$"]

    }        """

    try:

        count = min(int(request.args.get('count', 3)), 10)  # Max 10

        length = max(8, min(int(request.args.get('length', 12)), 20))  # 8-20

            

        passwords = [generate_random_password(length) for _ in range(count)]

            

        return jsonify({

            'success': True,

            'passwords': passwords

        }), 200

        

    except Exception as e:

        return jsonify({

            'success': False,

            'error': str(e)

        }), 500



#--------------- Student Management ---------------

@admin_bp.route('/students')

@login_required

def view_students():

    students = User.query.filter_by(role='student').join(StudentProfile).all()

    return render_template('admin/view_students.html', students=students)



@admin_bp.route('/quizzes')

@login_required

def manage_quizzes():

    if not is_superadmin_or_academic_admin():

        abort(403)

    quizzes = Quiz.query.order_by(Quiz.start_datetime.desc()).all()

    now = datetime.utcnow()



    upcoming = [q for q in quizzes if q.start_datetime > now]

    ongoing = [q for q in quizzes if q.start_datetime <= now <= q.end_datetime]

    past = [q for q in quizzes if q.end_datetime < now]



    return render_template(

        'admin/manage_quizzes.html',

        quizzes=quizzes,

        now=now,

        upcoming_count=len(upcoming),

        ongoing_count=len(ongoing),

        past_count=len(past)

    )



@admin_bp.route('/edit/<model>/<int:record_id>', methods=['GET', 'POST'])

@login_required

def edit_record(model, record_id):

    # Only handle models that exist

    if model == 'students':

        record = StudentProfile.query.get_or_404(record_id)

    elif model == 'teachers':

        record = TeacherProfile.query.get_or_404(record_id)

    else:

        abort(404)



    classes = get_programme_choices()



    if request.method == 'POST':

        # Update record fields dynamically

        for key, value in request.form.items():

            if hasattr(record, key):

                setattr(record, key, value)

        db.session.commit()

        flash(f"{model.capitalize()} updated successfully.", "success")

        return redirect(url_for('admin.list_records', model=model))



    # Render edit template

    return render_template('admin/edit_record.html', record=record, classes=classes, model=model)





def generate_quiz_backup_file(quiz_data, questions_data, backup_dir='quiz_backups'):

    os.makedirs(backup_dir, exist_ok=True)



    filename_base = f"{quiz_data['title'].replace(' ', '_')}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"



    json_path = os.path.join(backup_dir, f"{filename_base}.json")

    with open(json_path, 'w', encoding='utf-8') as f:

        json.dump({'quiz': quiz_data, 'questions': questions_data}, f, indent=4)



    csv_path = os.path.join(backup_dir, f"{filename_base}.csv")

    with open(csv_path, mode='w', newline='', encoding='utf-8') as csv_file:

        writer = csv.writer(csv_file)

        writer.writerow(['Question', 'Option', 'Is Correct'])



        for question in questions_data:

            q_text = question['text']

            for opt in question['options']:

                writer.writerow([q_text, opt['text'], 'TRUE' if opt['is_correct'] else 'FALSE'])



    return json_path  # or return both paths if needed



@admin_bp.route('/add_quiz', methods=['GET', 'POST'])

@login_required

def add_quiz():

    if not is_superadmin_or_academic_admin():

        abort(403)



    form = QuizForm()

    form.assigned_class.choices = get_programme_choices()



    selected_programme = request.form.get('assigned_class') or form.assigned_class.data

    if selected_programme:

        form.course_name.choices = get_course_choices(selected_programme)

    else:

        form.course_name.choices = []



    if not form.validate_on_submit():

        return render_template('admin/add_quiz.html', form=form)



    try:

        # BASIC FIELDS

        assigned_programme = form.assigned_class.data

        title = form.title.data.strip()

        start_datetime = form.start_datetime.data

        end_datetime = form.end_datetime.data

        duration = int(form.duration.data)



        # COURSE (CRITICAL FIX)

        course_id = request.form.get('course_id', type=int)

        if not course_id:

            flash("Please select a valid course.", "danger")

            return redirect(request.url)



        course = Course.query.get(course_id)

        if not course:

            flash("Selected course does not exist.", "danger")

            return redirect(request.url)



        # DUPLICATE TITLE CHECK FIX

        if Quiz.query.filter_by(title=title, course_id=course.id, assigned_class=assigned_programme).first():

            flash("A quiz with this title already exists for this course and programme.", "danger")

            return redirect(request.url)



        # TIME OVERLAP CHECK

        overlap = Quiz.query.filter(

            Quiz.assigned_class == assigned_programme,

            Quiz.start_datetime < end_datetime,

            Quiz.end_datetime > start_datetime

        ).first()



        if overlap:

            flash("Another quiz is already scheduled during this time.", "danger")

            return redirect(request.url)



        # CREATE QUIZ

        quiz = Quiz(

            assigned_class=assigned_programme,

            course_id=course.id,

            course_name=course.name,

            title=title,

            date=start_datetime.date(),

            start_datetime=start_datetime,

            end_datetime=end_datetime,

            duration_minutes=duration,

        )



        # FILE UPLOAD

        content_file = request.files.get('content_file')

        if content_file and content_file.filename and allowed_file(content_file.filename):

            filename = secure_filename(content_file.filename)

            os.makedirs(UPLOAD_FOLDER, exist_ok=True)

            content_file.save(os.path.join(UPLOAD_FOLDER, filename))

            quiz.content_file = filename



        db.session.add(quiz)

        db.session.flush()  # get quiz.id safely



        # SAVE QUESTIONS

        for key in request.form:

            if not re.match(r'^questions\[\d+\]\[text\]$', key):

                continue



            q_index = key.split('[')[1].split(']')[0]

            question_text = request.form.get(key, '').strip()

            if not question_text:

                continue



            blanks = re.findall(r'_{3,}', question_text)

            q_type = 'fill_in' if blanks else request.form.get(

                f'questions[{q_index}][type]', 'mcq'

            )



            question = Question(

                quiz_id=quiz.id,

                text=question_text,

                question_type=q_type

            )

            db.session.add(question)

            db.session.flush()



            # MULTIPLE CHOICE

            if q_type == 'mcq':

                o_index = 0

                while True:

                    text_key = f'questions[{q_index}][options][{o_index}][text]'

                    correct_key = f'questions[{q_index}][options][{o_index}][is_correct]'



                    if text_key not in request.form:

                        break



                    opt_text = request.form.get(text_key, '').strip()

                    if opt_text:

                        db.session.add(Option(

                            question_id=question.id,

                            text=opt_text,

                            is_correct=(correct_key in request.form)

                        ))

                    o_index += 1



            # FILL IN THE BLANK

            elif q_type == 'fill_in':

                a_index = 0

                while True:

                    ans_key = f'questions[{q_index}][answers][{a_index}]'

                    if ans_key not in request.form:

                        break



                    ans = request.form.get(ans_key, '').strip()

                    if ans:

                        db.session.add(Option(

                            question_id=question.id,

                            text=ans,

                            is_correct=True

                        ))

                    a_index += 1



        db.session.commit()

        flash("Quiz created successfully!", "success")

        return redirect(url_for('admin.manage_quizzes'))



    except Exception as e:

        db.session.rollback()

        flash(f"Error saving quiz: {e}", "danger")

        return redirect(request.url)



def is_quiz_active(quiz):

    now = datetime.now()

    quiz_start = datetime.combine(quiz.date, quiz.start_time)

    quiz_end = quiz_start + timedelta(minutes=quiz.duration_minutes)

    return quiz_start <= now <= quiz_end



@admin_bp.route('/edit_quiz/<int:quiz_id>', methods=['GET', 'POST'])

@login_required

def edit_quiz(quiz_id):

    """Edit an existing quiz - tertiary education (programme/level based)"""

    if not is_superadmin_or_academic_admin():

        abort(403)

    quiz = Quiz.query.get_or_404(quiz_id)

    form = QuizForm(obj=quiz)



    # GET

    if request.method == 'GET':

        form.course_id.data = quiz.course_id

        

        return render_template(

            'teacher/edit_quiz.html',

            form=form,

            quiz=quiz,

            selected_course_id=quiz.course_id

        )



    # POST

    if not form.validate_on_submit():

        return render_template(

            'teacher/edit_quiz.html',

            form=form,

            quiz=quiz

        )



    try:

        # BASIC FIELDS

        title = form.title.data.strip()

        start_datetime = form.start_datetime.data

        end_datetime = form.end_datetime.data

        duration = int(form.duration.data)



        if end_datetime <= start_datetime:

            flash("Invalid start and end time.", "danger")

            return redirect(request.url)



        # COURSE

        course_id = request.form.get('course_id', type=int)

        if not course_id:

            flash("Please select a valid course.", "danger")

            return redirect(request.url)



        course = Course.query.get(course_id)

        if not course:

            flash("Selected course does not exist.", "danger")

            return redirect(request.url)



        # Get programme/level from course

        programme_name = course.programme_name

        programme_level = course.programme_level



        # DUPLICATE TITLE CHECK (by programme, level, and course)

        if Quiz.query.filter(

            Quiz.id != quiz.id,

            Quiz.title == title,

            Quiz.course_id == course.id,

            Quiz.programme_name == programme_name,

            Quiz.programme_level == programme_level

        ).first():

            flash("A quiz with this title already exists for this course.", "danger")

            return redirect(request.url)



        # TIME OVERLAP CHECK (by programme and level)

        overlap = Quiz.query.filter(

            Quiz.id != quiz.id,

            Quiz.programme_name == programme_name,

            Quiz.programme_level == programme_level,

            Quiz.start_datetime < end_datetime,

            Quiz.end_datetime > start_datetime

        ).first()



        if overlap:

            flash("Another quiz is already scheduled during this time for this programme/level.", "danger")

            return redirect(request.url)



        # UPDATE QUIZ

        quiz.course_id = course.id

        quiz.course_name = course.name

        quiz.programme_name = programme_name

        quiz.programme_level = programme_level

        quiz.title = title

        quiz.start_datetime = start_datetime

        quiz.end_datetime = end_datetime

        quiz.date = start_datetime.date()

        quiz.duration_minutes = duration



        # DELETE OLD QUESTIONS

        for q in quiz.questions:

            Option.query.filter_by(question_id=q.id).delete()

        Question.query.filter_by(quiz_id=quiz.id).delete()

        db.session.flush()



        # REBUILD QUESTIONS

        for key in request.form:

            if not re.match(r'^questions\[\d+\]\[text\]$', key):

                continue



            q_index = key.split('[')[1].split(']')[0]

            q_text = request.form.get(key, '').strip()

            if not q_text:

                continue



            blanks = re.findall(r'_{3,}', q_text)

            q_type = 'fill_in' if blanks else request.form.get(

                f'questions[{q_index}][type]', 'mcq'

            )



            question = Question(

                quiz_id=quiz.id,

                text=q_text,

                question_type=q_type,

                points=1.0  # ✅ Set default points

            )

            db.session.add(question)

            db.session.flush()



            # ✅ SET correct_option_id FOR MCQ

            if q_type == 'mcq':

                options = []

                o_index = 0

                correct_option_id = None

                

                while True:

                    t_key = f'questions[{q_index}][options][{o_index}][text]'

                    c_key = f'questions[{q_index}][options][{o_index}][is_correct]'

                    if t_key not in request.form:

                        break



                    text = request.form.get(t_key, '').strip()

                    is_correct = c_key in request.form

                    

                    if text:

                        option = Option(

                            question_id=question.id,

                            text=text,

                            is_correct=is_correct

                        )

                        db.session.add(option)

                        db.session.flush()

                        

                        # ✅ TRACK THE CORRECT OPTION ID

                        if is_correct:

                            correct_option_id = option.id

                    

                    o_index += 1

                

                # ✅ ASSIGN correct_option_id TO QUESTION

                question.correct_option_id = correct_option_id



            elif q_type == 'fill_in':

                a_index = 0

                while True:

                    a_key = f'questions[{q_index}][answers][{a_index}]'

                    if a_key not in request.form:

                        break



                    ans = request.form.get(a_key, '').strip()

                    if ans:

                        option = Option(

                            question_id=question.id,

                            text=ans,

                            is_correct=True

                        )

                        db.session.add(option)

                    a_index += 1



        db.session.commit()

        flash("Quiz updated successfully!", "success")

        return redirect(url_for('admin.manage_quizzes'))



    except Exception as e:

        db.session.rollback()

        current_app.logger.exception(f"Error editing quiz {quiz_id}: {e}")

        flash(f"Error updating quiz: {e}", "danger")

        return redirect(request.url)

    

@admin_bp.route('/quizzes/delete/<int:quiz_id>', methods=['POST'])

@login_required

def delete_quiz(quiz_id):

    if not is_superadmin_or_academic_admin():

        abort(403)

    quiz = Quiz.query.get_or_404(quiz_id)

    db.session.delete(quiz)

    db.session.commit()

    flash("Quiz deleted successfully.", "success")

    return redirect(url_for('admin.manage_quizzes'))



@admin_bp.route('/restore_quiz', methods=['GET', 'POST'])

@login_required

def restore_quiz():

    check_admin_access()

    if request.method == 'POST':

        file = request.files.get('backup_file')

        if not file or not file.filename.endswith('.json'):

            flash("Please upload a valid JSON backup file.", "danger")

            return redirect(request.url)



        try:

            data = json.load(file)

            quiz_data = data.get('quiz')

            questions_data = data.get('questions', [])



            # Prevent duplicate

            if Quiz.query.filter_by(title=quiz_data['title'], assigned_class=quiz_data['assigned_class']).first():

                flash("A quiz with this title already exists.", "danger")

                return redirect(request.url)



            course = Course.query.filter_by(name=quiz_data['course_name']).first()

            if not course:

                flash("Course from backup does not exist.", "danger")

                return redirect(request.url)



            quiz = Quiz(

                course_id=course.id,

                course_name=quiz_data['course_name'],

                title=quiz_data['title'],

                assigned_class=quiz_data['assigned_class'],

                start_datetime=datetime.fromisoformat(quiz_data['start_datetime']),

                end_datetime=datetime.fromisoformat(quiz_data['end_datetime']),

                duration_minutes=int(quiz_data['duration_minutes']),

                attempts_allowed=int(quiz_data['attempts_allowed']),

                content_file=quiz_data.get('content_file')

            )

            db.session.add(quiz)

            db.session.flush()



            for q in questions_data:

                blanks = re.findall(r'_{3,}', q['text'])

                q_type = 'fill_in' if blanks else q.get('question_type', 'mcq')

                question = Question(quiz_id=quiz.id, text=q['text'], question_type=q_type)

                db.session.add(question)

                db.session.flush()

                for opt in q.get('options', []):

                    db.session.add(Option(question_id=question.id, text=opt['text'], is_correct=opt['is_correct']))



            db.session.commit()

            flash("Quiz restored successfully from backup.", "success")

            return redirect(url_for('admin.manage_quizzes'))



        except Exception as e:

            db.session.rollback()

            flash(f"Error restoring quiz: {e}", "danger")

            return redirect(request.url)



    return render_template("admin/restore_quiz.html")








# Admin: list timetable entries

"""

Exam Timetable Management Routes for Admin

"""



@admin_bp.route('/exam-timetable', methods=['GET', 'POST'])

@login_required

def admin_exam_timetable():

    """List and manage exam timetable entries."""

    if not is_superadmin_or_academic_admin():

        abort(403)

    

    if request.method == 'POST':

        try:

            programme_name = request.form.get('programme_name')

            programme_level = request.form.get('programme_level')

            course = request.form.get('course')

            date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()

            start_time = datetime.strptime(request.form.get('start_time'), '%H:%M').time()

            end_time = datetime.strptime(request.form.get('end_time'), '%H:%M').time()

            room = request.form.get('room', '').strip() or None

            building = request.form.get('building', '').strip() or None

            floor = request.form.get('floor', '').strip() or None

            notes = request.form.get('notes', '').strip() or None



            # Validate times

            if start_time >= end_time:

                flash("Start time must be before end time.", "danger")

                return redirect(url_for('admin.admin_exam_timetable'))



            # Check for conflicts

            conflict = ExamTimetableEntry.query.filter(

                ExamTimetableEntry.programme_name == programme_name,

                ExamTimetableEntry.programme_level == programme_level,

                ExamTimetableEntry.date == date,

                ExamTimetableEntry.start_time < end_time,

                ExamTimetableEntry.end_time > start_time

            ).first()



            if conflict:

                flash("Time slot conflicts with existing entry for this programme/level.", "danger")

                return redirect(url_for('admin.admin_exam_timetable'))



            # Create entry

            entry = ExamTimetableEntry(

                programme_name=programme_name,

                programme_level=programme_level,

                course=course,

                date=date,

                start_time=start_time,

                end_time=end_time,

                room=room,

                building=building,

                floor=floor,

                notes=notes

            )



            db.session.add(entry)

            db.session.commit()

            

            current_app.logger.info(f"Exam timetable entry created: {programme_name} Level {programme_level} - {course}")

            flash(f"Entry added: {programme_name} Level {programme_level}", "success")

            return redirect(url_for('admin.admin_exam_timetable'))



        except Exception as e:

            db.session.rollback()

            current_app.logger.exception("Failed creating exam timetable entry")

            flash(f"Error: {str(e)}", "danger")

            return redirect(url_for('admin.admin_exam_timetable'))



    # GET: List all entries

    entries = ExamTimetableEntry.query.order_by(

        ExamTimetableEntry.date.asc(),

        ExamTimetableEntry.start_time.asc()

    ).all()



    # Fetch available programmes, levels, semesters and courses for the template

    # programmes: list of programme names

    programme_choices = get_programme_choices() or []

    programmes = [p for p, _ in programme_choices]



    # levels: list of level strings

    level_choices = get_level_choices() or []

    levels = [l for l, _ in level_choices]



    # semesters: distinct semesters from Course model

    semesters_q = db.session.query(Course.semester).distinct().order_by(Course.semester).all()

    semesters = [s[0] for s in semesters_q if s and s[0]]



    # courses: full course list (dicts) to allow client-side filtering

    courses_q = Course.query.order_by(Course.name).all()

    courses = []

    courses_map = {}

    for c in courses_q:

        cd = {

            'code': c.code,

            'name': c.name,

            'programme_name': c.programme_name,

            'programme_level': c.programme_level,

            'semester': c.semester

        }

        courses.append(cd)

        courses_map[c.code] = cd



    return render_template('admin/exam_timetable_list.html', entries=entries,

                           programmes=programmes, levels=levels,

                           semesters=semesters, courses=courses, courses_map=courses_map)





@admin_bp.route('/exam-timetable/<int:entry_id>/edit', methods=['GET', 'POST'])

@login_required

@require_admin

def edit_exam_timetable_entry(entry_id):

    """Edit an exam timetable entry."""

    entry = ExamTimetableEntry.query.get_or_404(entry_id)



    if request.method == 'POST':

        try:

            entry.programme_name = request.form.get('programme_name')

            entry.programme_level = request.form.get('programme_level')

            entry.course = request.form.get('course')

            entry.date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()

            entry.start_time = datetime.strptime(request.form.get('start_time'), '%H:%M').time()

            entry.end_time = datetime.strptime(request.form.get('end_time'), '%H:%M').time()

            entry.room = request.form.get('room', '').strip() or None

            entry.building = request.form.get('building', '').strip() or None

            entry.floor = request.form.get('floor', '').strip() or None

            entry.notes = request.form.get('notes', '').strip() or None



            # Validate times

            if entry.start_time >= entry.end_time:

                flash("Start time must be before end time.", "danger")

                return redirect(url_for('admin.admin_exam_timetable'))



            # Check for conflicts (excluding current entry)

            conflict = ExamTimetableEntry.query.filter(

                ExamTimetableEntry.id != entry_id,

                ExamTimetableEntry.programme_name == entry.programme_name,

                ExamTimetableEntry.programme_level == entry.programme_level,

                ExamTimetableEntry.date == entry.date,

                ExamTimetableEntry.start_time < entry.end_time,

                ExamTimetableEntry.end_time > entry.start_time

            ).first()



            if conflict:

                flash("Time slot conflicts with another entry.", "danger")

                return redirect(url_for('admin.admin_exam_timetable'))



            db.session.commit()

            current_app.logger.info(f"Exam timetable entry {entry_id} updated")

            flash("Entry updated successfully.", "success")

            return redirect(url_for('admin.admin_exam_timetable'))



        except Exception as e:

            db.session.rollback()

            current_app.logger.exception("Failed updating exam timetable entry")

            flash(f"Error: {str(e)}", "danger")

            return redirect(url_for('admin.admin_exam_timetable'))



    return render_template('admin/edit_exam_timetable_entry.html', entry=entry)





@admin_bp.route('/exam-timetable/<int:entry_id>/delete', methods=['POST'])

@login_required

@require_admin

def delete_exam_timetable_entry(entry_id):

    """Delete an exam timetable entry."""

    entry = ExamTimetableEntry.query.get_or_404(entry_id)



    try:

        db.session.delete(entry)

        db.session.commit()

        current_app.logger.info(f"Exam timetable entry {entry_id} deleted")

        flash("Entry deleted successfully.", "success")

    except Exception as e:

        db.session.rollback()

        current_app.logger.exception("Failed deleting exam timetable entry")

        flash(f"Error: {str(e)}", "danger")



    return redirect(url_for('admin.admin_exam_timetable'))



# Manage Calendar Page

@admin_bp.route('/manage-events', methods=['GET', 'POST'])

@login_required

def manage_events():

    #admin_only()



    break_types = {

        'Holiday': 'Public Holiday',

        'Vacation': 'Vacation Break',

        'Exam': 'Examination Period',

        'Midterm': 'Midterm Break',

        'Other': 'Other Activity'

    }



    def parse_date_field(val, field_name=None):

        """Return a datetime.date or None. If invalid, return None."""

        if not val:

            return None

        try:

            return datetime.strptime(val, '%Y-%m-%d').date()

        except ValueError:

            if field_name:

                flash(f"Invalid date format for {field_name}. Please use YYYY-MM-DD.", "danger")

            return None



    # Load or initialize academic year

    year = AcademicYear.query.first()



    if request.method == 'POST':

        if not year:

            year = AcademicYear()

            db.session.add(year)



        # Parse all incoming form fields into date objects (or None)

        sd = parse_date_field(request.form.get('start_date'), 'Academic Year Start')

        ed = parse_date_field(request.form.get('end_date'), 'Academic Year End')

        s1s = parse_date_field(request.form.get('semester_1_start'), 'Semester 1 Start')

        s1e = parse_date_field(request.form.get('semester_1_end'), 'Semester 1 End')

        s2s = parse_date_field(request.form.get('semester_2_start'), 'Semester 2 Start')

        s2e = parse_date_field(request.form.get('semester_2_end'), 'Semester 2 End')



        # Basic server-side validation: required fields must be provided and valid

        required_ok = all([sd, ed, s1s, s1e, s2s, s2e])

        if not required_ok:

            flash("Please provide valid dates for all academic year fields.", "danger")

            # Don't commit; re-render form with current year (possibly None)

            # NOTE: we intentionally fall through to render_template below so the user can fix input

        else:

            # Assign Python date objects to model fields (SQLAlchemy Date expects date objects)

            year.start_date = sd

            year.end_date = ed

            year.semester_1_start = s1s

            year.semester_1_end = s1e

            year.semester_2_start = s2s

            year.semester_2_end = s2e



            try:

                db.session.commit()

                flash("Academic Year settings saved.", "success")

                return redirect(url_for('admin.manage_events'))

            except Exception as exc:

                db.session.rollback()

                # Log exc if you have logging available; here we notify user

                flash("Error saving academic year. Please check server logs.", "danger")



    # Load calendar events

    calendar_events = AcademicCalendar.query.order_by(AcademicCalendar.date).all()

    cal_events = [{

        'id': e.id,

        'title': e.label,

        'start': e.date.isoformat(),

        'backgroundColor': '#28a745' if e.is_workday else '#dc3545',

        'break_type': e.break_type

    } for e in calendar_events]



    # Add semester background ranges only if dates exist

    if year and year.semester_1_start and year.semester_1_end:

        cal_events.append({

            'start': year.semester_1_start.isoformat(),

            'end': (year.semester_1_end + timedelta(days=1)).isoformat(),

            'display': 'background',

            'color': '#d1e7dd',

            'title': 'Semester 1'

        })

    if year and year.semester_2_start and year.semester_2_end:

        cal_events.append({

            'start': year.semester_2_start.isoformat(),

            'end': (year.semester_2_end + timedelta(days=1)).isoformat(),

            'display': 'background',

            'color': '#f8d7da',

            'title': 'Semester 2'

        })



    return render_template('admin/manage_events.html',

                           cal_events=cal_events,

                           break_types=break_types,

                           academic_year=year)



# Add new event

@admin_bp.route('/events/add', methods=['POST'])

@login_required

def add_event():

    check_admin_access()

    date = request.form.get('date')

    label = request.form.get('label')

    break_type = request.form.get('break_type')

    is_workday = bool(request.form.get('is_workday'))

    if not date or not label or not break_type:

        return "Missing fields", 400



    calendar = AcademicCalendar(

        date=datetime.strptime(date, '%Y-%m-%d').date(),

        label=label,

        break_type=break_type,

        is_workday=is_workday

    )

    db.session.add(calendar)

    db.session.commit()

    return '', 204



@admin_bp.route('/events/edit/<int:event_id>', methods=['POST'])

@login_required

def edit_event(event_id):

    check_admin_access()

    event = AcademicCalendar.query.get_or_404(event_id)

    event.date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()

    event.label = request.form.get('label')

    event.break_type = request.form.get('break_type')

    event.is_workday = bool(request.form.get('is_workday'))

    db.session.commit()

    return '', 204



@admin_bp.route('/events/delete/<int:event_id>', methods=['POST'])

@login_required

def delete_event(event_id):

    check_admin_access()

    event = AcademicCalendar.query.get_or_404(event_id)

    db.session.delete(event)

    db.session.commit()

    return '', 204





# API endpoint for calendar drag/drop or click

@admin_bp.route('/manage-events/json')

@login_required

def events_json():

    check_admin_access()

    events = AcademicCalendar.query.all()

    return jsonify([

      {'id': e.id,

       'title': e.label,

       'start': e.date.isoformat(),

       'color': e.is_workday and '#28a745' or '#dc3545'

      } for e in events

    ])



# API endpoint for academic calendar

@admin_bp.route('/academic-calendar')

@login_required

def academic_calendar():

    # (only for admins or teachers)

    today = date.today()

    # fetch for this year

    days = AcademicCalendar.query.filter(

        extract('year', AcademicCalendar.date) == today.year

    ).all()

    return jsonify([

        {'date': d.date.isoformat(), 'label': d.label}

        for d in days if not d.is_workday

    ])



@admin_bp.route('/profile')

@login_required

def profile():

    return render_template('admin/profile.html', user=current_user)



#========================== Database Management ==========================

# in your route

from models import *

from admissions.models import Applicant, Application, ApplicationDocument, AdmissionVoucher, ApplicationResult, ApplicationPayment



def serialize(obj):

    if hasattr(obj, 'to_dict'):

        return obj.to_dict()

    result = {}

    for c in obj.__table__.columns:

        val = getattr(obj, c.name)

        if isinstance(val, (datetime, date, time)):

            result[c.name] = val.isoformat()

        else:

            result[c.name] = val

    return result



def _pretty_model_name(cls):
    name = getattr(cls, '__name__', str(cls))
    out = []
    for i, ch in enumerate(name):
        if i and ch.isupper() and (name[i - 1].islower() or (i + 1 < len(name) and name[i + 1].islower())):
            out.append(' ')
        out.append(ch)
    return ''.join(out)


def get_models_dict():
    models = {}
    try:
        mappers = list(db.Model.registry.mappers)
    except Exception:
        mappers = []

    for mapper in mappers:
        cls = getattr(mapper, 'class_', None)
        if not cls:
            continue
        if not hasattr(cls, '__table__'):
            continue
        if not hasattr(cls, 'query'):
            continue

        pretty = _pretty_model_name(cls)
        if pretty not in models:
            models[pretty] = cls
    return models



@admin_bp.route("/settings/result-template", methods=["GET", "POST"])

def result_template_settings():

    from utils.results_manager import ResultManager



    templates = ResultManager.get_available_templates()

    current = ResultManager.get_template_name()



    if request.method == "POST":

        selected = request.form.get("template")

        ResultManager.set_template_name(selected)

        flash("Result template updated successfully!", "success")

        return redirect(url_for("admin.result_template_settings"))



    return render_template("admin/result_template_settings.html",

                           templates=templates, current=current)





# View all tables and records

@admin_bp.route('/database')

@login_required

def view_database():

    data = {}

    models = get_models_dict()

    for name, model in models.items():

        try:

            records = model.query.all()

            data[name] = [serialize(row) for row in records]  # <-- generic serializer

        except Exception as e:

            try:
                db.session.rollback()
            except Exception:
                pass

            # Skip models whose tables don't exist

            logger.warning(f"Skipping {name} table: {e}")

            data[name] = []

    return render_template('admin/database.html', data=data)



# Update a record (POST JSON with fields to update)

import re

from flask import jsonify, request

from datetime import datetime, date, time



# helper to map human model names to slug form used in the front-end

def slugify_model_name(name: str) -> str:

    # lowercase, replace non-alnum with underscore, collapse underscores

    s = re.sub(r'[^a-z0-9]+', '_', name.lower())

    s = re.sub(r'_+', '_', s).strip('_')

    return s



def resolve_model_from_slug(slug: str):

    MODELS = get_models_dict()

    # direct match first (in case you ever pass the exact key)

    if slug in MODELS:

        return MODELS[slug]



    # try matching by slugified key names

    for pretty_name, Model in MODELS.items():

        if slugify_model_name(pretty_name) == slug:

            return Model



    return None



@admin_bp.route('/update/<model>/<record_id>', methods=['POST'])

@login_required

def update_record(model, record_id):

    # resolve model slug -> SQLAlchemy model

    Model = resolve_model_from_slug(model)

    if not Model:

        return jsonify({"error": "Unknown model", "model": model}), 400



    # allow numeric or string primary keys; try to cast to int first

    try:

        pk = int(record_id)

    except Exception:

        pk = record_id



    record = Model.query.get(pk)

    if not record:

        return jsonify({"error": "Record not found", "model": model, "id": record_id}), 404



    data = request.get_json(silent=True)

    if not data:

        return jsonify({"error": "Missing JSON payload"}), 400



    # column name set for quick membership testing

    columns = {c.name.lower(): c for c in Model.__table__.columns}



    updated_fields = {}

    for key, value in data.items():

        key_l = key.lower()

        if key_l not in columns:

            # skip unknown columns (don't raise) — useful when payload contains unrelated keys

            continue



        col = columns[key_l]



        # conversion helpers

        def to_bool(v):

            if isinstance(v, bool): return v

            if v is None: return None

            vs = str(v).strip().lower()

            if vs in ('1','true','yes','y','on'): return True

            if vs in ('0','false','no','n','off',''): return False

            return bool(v)



        try:

            col_type = str(col.type).lower()

            if "boolean" in col_type:

                new_val = to_bool(value)

            elif "integer" in col_type:

                new_val = int(value) if (value != '' and value is not None) else None

            elif "float" in col_type or "numeric" in col_type or "decimal" in col_type:

                new_val = float(value) if (value != '' and value is not None) else None

            elif "date" in col_type or "time" in col_type:

                # accept ISO strings or simple date strings; try parsing

                if value in (None, ''):

                    new_val = None

                else:

                    try:

                        # datetime.fromisoformat handles 'YYYY-MM-DD' and full iso

                        new_val = datetime.fromisoformat(value)

                    except Exception:

                        # fallback: if only date, try date.fromisoformat

                        try:

                            new_val = date.fromisoformat(value)

                        except Exception:

                            # as last resort store raw string

                            new_val = value

            else:

                # default text / varchar

                new_val = value

        except Exception as e:

            # don't fail the whole update for one field; log and skip

            current_app.logger.warning(f"Failed to coerce field {key} -> {e}")

            continue



        try:

            setattr(record, key, new_val)

            updated_fields[key] = new_val

        except Exception as e:

            current_app.logger.warning(f"Failed to set attribute {key} on {Model}: {e}")



    try:

        db.session.commit()

    except Exception as e:

        db.session.rollback()

        current_app.logger.exception("DB commit failed during admin update")

        return jsonify({"error": "DB commit failed", "details": str(e)}), 500



    return jsonify(serialize(record)), 200



# Delete a record

@admin_bp.route('/delete/<model>/<int:record_id>', methods=['DELETE'])

@login_required

def delete_record(model, record_id):

    # Accept flexible model identifiers (case-insensitive, singular/plural, underscores/dashes)

    def resolve_model(key):

        MODELS = get_models_dict()

        # direct match

        if key in MODELS:

            return MODELS[key]

        # case-insensitive match

        for k, v in MODELS.items():

            if k.lower() == key.lower():

                return v

        # normalized forms: remove spaces/underscores/dashes

        nk = key.replace('-', ' ').replace('_', ' ').replace('  ', ' ').strip().lower()

        for k, v in MODELS.items():

            if k.replace(' ', '').lower() == nk.replace(' ', ''):

                return v

            if k.lower().replace(' ', '') == key.lower().replace(' ', ''):

                return v

        # try singular/plural heuristics (drop/add trailing 's')

        if key.endswith('s'):

            singular = key[:-1]

            for k, v in MODELS.items():

                if k.lower().startswith(singular.lower()):

                    return v

        else:

            plural = key + 's'

            for k, v in MODELS.items():

                if k.lower().startswith(plural.lower()):

                    return v

        return None



    Model = resolve_model(model)

    if not Model:

        return jsonify({"error": f"Unknown model: {model}"}), 400

    record = Model.query.get(record_id)

    if not record:

        return f"Record with ID {record_id} not found.", 404



    db.session.delete(record)

    db.session.commit()

    return '', 204



#========================== Student Promotion ==========================

@admin_bp.route('/admin/promote-all-students', methods=['GET', 'POST'])

@login_required

def promote_all_students():

    """

    DEPRECATED ROUTE - Redirects to new promotion system

    

    This function existed before but was:

    ✗ Broken (used non-existent user_id field)

    ✗ Bad design (promoted ALL students at once)

    ✗ Used quiz scores (wrong metric)

    ✗ No vetting or filtering

    

    NOW: Redirects to professional yearly promotion system

    """

    flash(

        "The old single-click promotion is deprecated. "

        "Using the new Management System for safer, vetted promotions.",

        "info"

    )

    return redirect(url_for('admin.manage_promotions'))



@admin_bp.route('/manage-promotions', methods=['GET', 'POST'])

@login_required

def manage_promotions():

    """

    Manage student promotions with filtering and vetting

    

    Features:

    - Filter by academic year, programme, level

    - Individual review of each student

    - Approve/reject before promotion

    - Bulk promotion of approved students

    - Complete audit trail

    """

    

    if request.method == 'GET':

        # Show promotion management page

        settings = SchoolSettings.query.first()

        current_year = settings.current_academic_year if settings else None

        

        # Get available academic years
        academic_years = db.session.query(StudentCourseGrade.academic_year)\
            .distinct()\
            .order_by(StudentCourseGrade.academic_year.desc())\
            .all()

        academic_years = [year[0] for year in academic_years if year[0]]

        

        # Get programmes

        programmes = db.session.query(

            StudentProfile.current_programme

        ).distinct().all()

        programmes = [

            {'programme_code': p[0], 'programme_name': p[0]}

            for p in programmes if p[0]

        ]

        ensure_promotion_tables()
        # Get courses

        courses = Course.query.all()

        

        return render_template(

            'admin/promotions.html',

            current_year=current_year,

            academic_years=academic_years,

            programmes=programmes,

            courses=courses

        )

    

    return redirect(url_for('admin.manage_promotions'))





# =====================================================

# HELPER FUNCTIONS FOR PROMOTION SYSTEM

# =====================================================



def calculate_yearly_gpa(student_id, academic_year):

    """

    Calculate GPA from ACADEMIC GRADES (not quiz scores!)

    

    This replaces the old calculate_student_score() which:

    ✗ Used quiz submissions (wrong metric)

    ✗ Had broken query

    ✗ Wasn't reliable

    

    Args:

        student_id: User ID (string like "STD001")

        academic_year: Academic year (string like "2024")

    

    Returns:

        GPA as float (0.0 to 4.0)

    """

    

    # Get all FINALIZED course grades for the year (both semesters).

    # If the model has `is_finalized`, use it; otherwise treat non-NULL `final_score` as finalized.

    if hasattr(StudentCourseGrade, 'is_finalized'):

        grades = StudentCourseGrade.query.filter(

            StudentCourseGrade.student_id == student_id,

            StudentCourseGrade.academic_year == academic_year,

            StudentCourseGrade.is_finalized == True

        ).all()

    else:

        grades = StudentCourseGrade.query.filter(

            StudentCourseGrade.student_id == student_id,

            StudentCourseGrade.academic_year == academic_year,

            StudentCourseGrade.final_score != None

        ).all()

    

    if not grades:

        return 0.0

    

    # Grade point mapping

    grade_points = {

        'A': 4.0, 'A-': 3.7,

        'B+': 3.3, 'B': 3.0, 'B-': 2.7,

        'C+': 2.3, 'C': 2.0, 'C-': 1.7,

        'D+': 1.3, 'D': 1.0, 'F': 0.0

    }

    

    total_points = 0

    total_credits = 0

    

    for grade in grades:

        points = grade_points.get(grade.grade_letter, 0)

        credit_hours = grade.course.credit_hours if grade.course else 3

        total_points += points * credit_hours

        total_credits += credit_hours

    

    return round(total_points / total_credits, 2) if total_credits > 0 else 0.0





def get_promotion_candidates(academic_year, filters=None):

    """

    Get students eligible for promotion with optional filtering

    

    Args:

        academic_year: The year to process

        filters: Dict with optional keys:

            - programmes: list of programme codes

            - levels: list of level strings

            - min_gpa: minimum GPA threshold

            - statuses: list of academic statuses

    

    Returns:

        List of candidate dicts with student info and GPA

    """

    

    filters = filters or {}

    

    # Start with all students

    query = StudentProfile.query.filter_by(academic_status='Active')

    

    # Apply filters

    if filters.get('programmes'):

        query = query.filter(StudentProfile.current_programme.in_(filters['programmes']))

    

    if filters.get('levels'):

        query = query.filter(StudentProfile.programme_level.in_(
            [int(level) for level in filters['levels']]
        ))

    if filters.get('statuses'):
        query = query.filter(StudentProfile.academic_status.in_(filters['statuses']))

    

    students = query.all()

    

    candidates = []

    for student in students:

        # Calculate yearly GPA

        gpa = calculate_yearly_gpa(student.user_id, academic_year)

        

        # Filter by minimum GPA if specified

        if filters.get('min_gpa') and gpa < filters['min_gpa']:

            continue

        

        user = User.query.filter_by(user_id=student.user_id).first()

        

        candidates.append({

            'student_id': student.user_id,

            'name': user.full_name if user else 'Unknown',

            'programme': student.current_programme,

            'level': student.programme_level,

            'gpa': gpa,

            'vetting_status': student.vetting_status or 'pending'

        })

    

    return sorted(candidates, key=lambda x: x['gpa'], reverse=True)





# =====================================================

# API ENDPOINTS FOR AJAX CALLS

# =====================================================



@admin_bp.route('/promotion-candidates', methods=['POST'])

@login_required

def api_get_promotion_candidates():

    """API endpoint to get promotion candidates"""

    

    academic_year = request.form.get('academic_year')

    if not academic_year:

        return jsonify({'error': 'Academic year required'}), 400

    

    filters = {

        'programmes': request.form.getlist('programme'),

        'levels': request.form.getlist('level'),

        'min_gpa': float(request.form.get('min_gpa', 0)) or None,

        'statuses': request.form.getlist('status')

    }

    

    candidates = get_promotion_candidates(academic_year, filters)

    

    return jsonify({

        'total': len(candidates),

        'candidates': candidates,

        'stats': {

            'avg_gpa': round(sum(c['gpa'] for c in candidates) / len(candidates), 2) if candidates else 0,

            'approved': sum(1 for c in candidates if c['vetting_status'] == 'approved'),

            'pending': sum(1 for c in candidates if c['vetting_status'] == 'pending'),

            'rejected': sum(1 for c in candidates if c['vetting_status'] == 'rejected')

        }

    })


@admin_bp.route('/promotion-candidates/<student_id>', methods=['GET'])
@login_required
def api_get_student_vetting_details(student_id):
    """Return the student details required by the promotion review modal."""
    student = StudentProfile.query.filter_by(user_id=student_id).first_or_404()
    user = User.query.filter_by(user_id=student_id).first()
    return jsonify({
        'student_id': student_id,
        'name': user.full_name if user else 'Unknown',
        'programme': student.current_programme,
        'gpa': calculate_yearly_gpa(student_id, request.args.get('academic_year'))
        if request.args.get('academic_year') else 0,
        'courses': [],
    })





@admin_bp.route('/student/approve-promotion', methods=['POST'])

@login_required

def api_approve_promotion():

    """Approve a student for promotion"""

    

    data = request.get_json()

    student_id = data.get('student_id')

    

    student = StudentProfile.query.filter_by(user_id=student_id).first()

    if not student:

        return jsonify({'error': 'Student not found'}), 404

    

    student.vetting_status = 'approved'

    db.session.commit()

    

    return jsonify({'success': True, 'message': f'Student {student_id} approved'})





@admin_bp.route('/student/reject-promotion', methods=['POST'])

@login_required

def api_reject_promotion():

    """Reject a student from promotion"""

    

    data = request.get_json()

    student_id = data.get('student_id')

    reason = data.get('reason', '')

    

    student = StudentProfile.query.filter_by(user_id=student_id).first()

    if not student:

        return jsonify({'error': 'Student not found'}), 404

    

    student.vetting_status = 'rejected'

    student.rejection_reason = reason

    db.session.commit()

    

    return jsonify({'success': True, 'message': f'Student {student_id} rejected'})





@admin_bp.route('/students/bulk-promote', methods=['POST'])

@login_required

def api_bulk_promote_students():

    """Promote multiple approved students"""

    

    data = request.get_json()

    student_ids = data.get('student_ids', [])

    academic_year = data.get('academic_year')

    

    if not academic_year:

        return jsonify({'error': 'Academic year required'}), 400

    

    promoted = 0

    errors = []

    

    try:

        for student_id in student_ids:

            student = StudentProfile.query.filter_by(user_id=student_id).first()

            

            if not student:

                errors.append({'student_id': student_id, 'error': 'Not found'})

                continue

            

            if student.vetting_status != 'approved':

                errors.append({'student_id': student_id, 'error': 'Not approved'})

                continue

            

            # Get yearly GPA

            gpa = calculate_yearly_gpa(student_id, academic_year)

            

            # Advance to next level

            current_level = int(student.programme_level or 100)

            new_level = current_level + 100

            

            # Update student

            student.programme_level = str(new_level)

            student.academic_status = 'Active'

            student.vetting_status = 'promoted'

            

            # Create audit record

            promotion = StudentPromotion(

                student_id=student_id,

                from_level=str(current_level),

                to_level=str(new_level),

                gpa=gpa,

                academic_status='Active',

                academic_year=academic_year,

                promoted_by=current_user.id,

                promoted_at=datetime.utcnow()

            )

            

            db.session.add(promotion)

            promoted += 1

        

        db.session.commit()

        

        flash(f"Successfully promoted {promoted} students", "success")

        return jsonify({

            'success': True,

            'promoted': promoted,

            'errors': errors

        })

    

    except Exception as e:

        db.session.rollback()

        return jsonify({'success': False, 'error': str(e)}), 400

    



@admin_bp.route('/admin/download-backup/<filename>')

def download_backup(filename):

    return send_from_directory(directory='backups', path=filename, as_attachment=True)



#--------------- Assignment Management ---------------

@admin_bp.route('/manage-assignments')

@login_required

def manage_assignments():

    if not is_superadmin_or_academic_admin():

        abort(403)



    assignments = Assignment.query.order_by(Assignment.due_date.asc()).all()

    return render_template(

        'admin/manage_assignments.html',

        assignments=assignments,

        now=datetime.utcnow()   # ✅ datetime comparison works now

    )



@admin_bp.route('/assignments/add', methods=['GET', 'POST'])

@login_required

def add_assignment():

    if not is_superadmin_or_academic_admin():

        abort(403)



    form = AssignmentForm()



    if form.validate_on_submit():

        course_id = request.form.get('course_id', type=int)

        if not course_id:

            flash("Please select a valid course.", "danger")

            return redirect(request.url)



        course = Course.query.get(course_id)

        if not course:

            flash("Course not found.", "danger")

            return redirect(request.url)



        file = form.file.data

        filename, original_name = None, None

        if file:

            original_name = file.filename

            filename = secure_filename(original_name)

            file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))



        assignment = Assignment(

            title=form.title.data,

            description=form.description.data,

            instructions=form.instructions.data,

            course_id=course.id,          # ✅ REQUIRED

            course_name=course.name,

            programme_name=form.programme_name.data,

            programme_level=form.programme_level.data,

            due_date=form.due_date.data,

            filename=filename,

            original_name=original_name,

            max_score=form.max_score.data

        )



        db.session.add(assignment)

        db.session.commit()



        create_assignment_notification(assignment)

        flash('Assignment added successfully.', 'success')

        return redirect(url_for('admin.manage_assignments'))



    return render_template('admin/add_assignment.html', form=form)



def is_admin_or_teacher():

    return current_user.is_authenticated and current_user.role in ['admin', 'teacher']





@admin_bp.route('/assignments/edit/<int:assignment_id>', methods=['GET', 'POST'])

@login_required

def edit_assignment(assignment_id):

    if not is_superadmin_or_academic_admin():

        abort(403)



    assignment = Assignment.query.get_or_404(assignment_id)



    # Ensure teacher owns this assignment via course

    profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()

    if not profile or assignment.course not in [a.course for a in profile.assignments]:

        abort(403)



    form = AssignmentForm(obj=assignment)



    if form.validate_on_submit():

        assignment.title = form.title.data

        assignment.description = form.description.data

        assignment.instructions = form.instructions.data

        assignment.assigned_class = form.assigned_class.data

        assignment.due_date = form.due_date.data

        assignment.max_score = form.max_score.data



        # 🚫 course_id is NOT touched

        # ✅ course_name is kept in sync automatically

        assignment.course_name = assignment.course.name



        file = form.file.data

        if file:

            original_name = file.filename

            filename = secure_filename(original_name)

            file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)

            file.save(file_path)



            assignment.filename = filename

            assignment.original_name = original_name



        db.session.commit()

        flash('Assignment updated successfully.', 'success')

        return redirect(url_for('teacher.manage_assignments'))



    return render_template(

        'teacher/edit_assignment.html',

        form=form,

        assignment=assignment

    )



@admin_bp.route('/assignments/delete/<int:assignment_id>', methods=['POST'])

@login_required

def delete_assignment(assignment_id):

    if not is_superadmin_or_academic_admin():

        abort(403)



    assignment = Assignment.query.get_or_404(assignment_id)

    if assignment.filename:

        path = os.path.join(current_app.config['UPLOAD_FOLDER'], assignment.filename)

        if os.path.exists(path):

            os.remove(path)



    db.session.delete(assignment)

    db.session.commit()

    flash('Assignment deleted successfully.', 'success')

    return redirect(url_for('admin.manage_assignments'))



def send_notification(type, title, message, recipients, sender=None, related_type=None, related_id=None):

    """Create a notification and attach to recipients."""

    notif = Notification(

        type=type,

        title=title,

        message=message,

        sender_id=sender.user_id if sender else None,

        related_type=related_type,

        related_id=related_id

    )

    db.session.add(notif)

    db.session.flush()  # ensures notif.id is available



    # Create recipient links

    for user in recipients:

        db.session.add(NotificationRecipient(notification_id=notif.id, user_id=user.user_id))



    db.session.commit()

    return notif



#--------------- Course Materials Management ---------------

@admin_bp.route('/materials')

@login_required

def list_materials():

    materials = CourseMaterial.query.order_by(CourseMaterial.upload_date.desc()).all()

    return render_template('admin/manage_materials.html', materials=materials)



#--------------- Course Materials CRUD Operations ---------------

def get_programme_choices():

    """

    Return a list of (value, label) tuples for all programmes

    (certificate + diploma), so forms can call this dynamically.

    """

    # Certificate Programmes

    certificate_programmes = [

        'Early Childhood Education',

        'Dispensing Technician II & III',

        'Diagnostic Medical Sonography',

        'Medical Laboratory Technology',

        'Dispensing Assistant',

        'Health Information Management',

        'Optical Technician',

        'Cyber Security'

    ]



    # Diploma Programmes

    diploma_programmes = [

        'Early Childhood Education',

        'Midwifery',

        'Ophthalmic Dispensing',

        'Medical Laboratory Technology',

        'HND Dispensing Technology',

        'Health Information Management',

        'Diploma in Early Childhood Education'

    ]



    # Combine and remove duplicates

    all_programmes = sorted(set(certificate_programmes + diploma_programmes))

    return [(p, p) for p in all_programmes]





def get_course_choices(programme_name):

    """

    Return a list of (value, label) tuples for courses

    filtered by the selected programme.

    """

    from models import Course  # adjust import path if needed



    if not programme_name:

        return []



    courses = Course.query.filter_by(programme=programme_name).all()

    return [(c.id, c.name) for c in courses]





# Courses Management

@admin_bp.route('/courses', methods=['GET', 'POST'])

@login_required

@require_admin

def manage_courses():

    """List all courses (tertiary version with programme/level filtering)"""

    # Handle POST request for updating registration window
    if request.method == 'POST':
        registration_start = request.form.get('registration_start')
        registration_end = request.form.get('registration_end')
        
        if registration_start and registration_end:
            try:
                # Parse datetime strings
                start_dt = datetime.strptime(registration_start, '%Y-%m-%dT%H:%M')
                end_dt = datetime.strptime(registration_end, '%Y-%m-%dT%H:%M')
                
                # Update registration window using Course model method
                Course.set_registration_window(start_dt, end_dt)
                db.session.commit()
                
                flash("Course registration window updated successfully!", "success")
                
            except ValueError as e:
                flash("Invalid datetime format. Please use the correct format.", "danger")
            except Exception as e:
                db.session.rollback()
                flash(f"Error updating registration window: {str(e)}", "danger")
        else:
            flash("Both start and end dates are required.", "danger")
        
        return redirect(url_for('admin.manage_courses'))

    # Get current registration window
    registration_window = Course.get_registration_window()
    registration_start = registration_window[0] if registration_window and registration_window[0] else None
    registration_end = registration_window[1] if registration_window and registration_window[1] else None

    # Optional filtering by programme and level

    programme_filter = request.args.get('programme', '', type=str)

    level_filter = request.args.get('level', '', type=str)

    semester_filter = request.args.get('semester', '', type=str)

    year_filter = request.args.get('year', '', type=str)



    # Base query

    query = Course.query



    # Apply filters

    if programme_filter:

        query = query.filter_by(programme_name=programme_filter)

    

    if level_filter:

        query = query.filter_by(programme_level=level_filter)

    

    if semester_filter:

        query = query.filter_by(semester=semester_filter)

    

    if year_filter:

        query = query.filter_by(academic_year=year_filter)



    # Order by programme, level, then semester

    courses = query.order_by(

        Course.programme_name,

        Course.programme_level,

        Course.semester

    ).all()



    # Get unique values for filter dropdowns

    programmes = db.session.query(Course.programme_name).distinct().order_by(Course.programme_name).all()

    levels = db.session.query(Course.programme_level).distinct().order_by(Course.programme_level).all()

    semesters = db.session.query(Course.semester).distinct().order_by(Course.semester).all()

    years = db.session.query(Course.academic_year).distinct().order_by(Course.academic_year.desc()).all()



    return render_template(

        'admin/manage_courses.html',

        courses=courses,

        programmes=[p[0] for p in programmes if p[0]],

        levels=[l[0] for l in levels if l[0]],

        semesters=[s[0] for s in semesters if s[0]],

        years=[y[0] for y in years if y[0]],

        selected_programme=programme_filter,

        selected_level=level_filter,

        selected_semester=semester_filter,

        selected_year=year_filter,

        registration_start=registration_start,

        registration_end=registration_end

    )



@admin_bp.route('/courses/add', methods=['GET', 'POST'])

@login_required

@require_admin

def add_programme_course():

    """Add a new course linked to a programme and programme level."""



    form = CourseForm()

    

    if form.validate_on_submit():

        # Avoid duplicate course codes

        if Course.query.filter_by(code=form.code.data).first():

            flash("Course code already exists.", "danger")

            return render_template('admin/add_edit_programme_course.html', form=form)



        course = Course(

            name=form.name.data,

            code=form.code.data,

            programme_name=form.programme_name.data,

            programme_level=form.programme_level.data,

            semester=form.semester.data,

            academic_year=form.academic_year.data,

            credit_hours=form.credit_hours.data,

            is_mandatory=form.is_mandatory.data

        )

        db.session.add(course)

        db.session.commit()

        

        flash(f"✓ Course '{course.name}' added successfully!", "success")

        return redirect(url_for('admin.manage_courses'))

    

    return render_template('admin/add_edit_programme_course.html', form=form)



@admin_bp.route('/courses/edit/<int:course_id>', methods=['GET', 'POST'])

@login_required

@require_admin

def edit_course(course_id):

    """Edit an existing course (tertiary version)"""



    course = Course.query.get_or_404(course_id)

    form = CourseForm(obj=course)



    if form.validate_on_submit():

        # Check for duplicate code (excluding current course)

        existing = Course.query.filter(

            Course.code == form.code.data,

            Course.id != course_id

        ).first()

        

        if existing:

            flash("Course code already exists.", "danger")

            return render_template('admin/add_edit_programme_course.html', form=form, course=course)



        form.populate_obj(course)

        db.session.commit()

        

        flash(f"✓ Course '{course.name}' updated successfully!", "success")

        return redirect(url_for('admin.manage_courses'))



    return render_template('admin/add_edit_programme_course.html', form=form, course=course)



@admin_bp.route('/courses/delete/<int:course_id>', methods=['POST'])

@login_required

@require_admin

def delete_course(course_id):

    """Delete a course (tertiary version)"""



    course = Course.query.get_or_404(course_id)

    course_name = course.name

    

    try:

        db.session.delete(course)

        db.session.commit()

        flash(f"✓ Course '{course_name}' deleted successfully!", "success")

    except Exception as e:

        db.session.rollback()

        flash(f"Error deleting course: {e}", "danger")



    return redirect(url_for('admin.manage_courses'))



@admin_bp.route('/courses/<int:course_id>')

@login_required

@require_admin

def course_details(course_id):

    """View course details and registered students (tertiary version)"""



    course = Course.query.get_or_404(course_id)

    registrations = course.registrations  # Assuming relationship exists



    return render_template(

        'admin/course_details.html',

        course=course,

        registrations=registrations

    )



#–– Limits CRUD ––

@admin_bp.route('/courses/limits')

@login_required

@require_admin

def manage_limits():

    limits = CourseLimit.query.order_by(CourseLimit.class_level, CourseLimit.semester).all()

    return render_template('admin/manage_limits.html', limits=limits)



@admin_bp.route('/courses/limits/add', methods=['GET','POST'])

@login_required

@require_admin

def add_limit():

    form = CourseLimitForm()

    if form.validate_on_submit():

        lim = CourseLimit(

            class_level    = form.class_level.data,

            semester       = form.semester.data,

            academic_year  = form.academic_year.data,

            mandatory_limit= form.mandatory_limit.data,

            optional_limit = form.optional_limit.data

        )

        db.session.add(lim)

        db.session.commit()

        flash('Limits set.', 'success')

        return redirect(url_for('admin.manage_limits'))

    return render_template('admin/add_edit_limit.html', form=form)



@admin_bp.route('/courses/limits/edit/<int:limit_id>', methods=['GET','POST'])

@login_required

@require_admin

def edit_limit(limit_id):

    lim = CourseLimit.query.get_or_404(limit_id)

    form = CourseLimitForm(obj=lim)

    if form.validate_on_submit():

        form.populate_obj(lim)

        db.session.commit()

        flash('Limits updated.', 'success')

        return redirect(url_for('admin.manage_limits'))

    return render_template('admin/add_edit_limit.html', form=form, limit=lim)



@admin_bp.route('/courses/limits/delete/<int:limit_id>', methods=['POST'])

@login_required

@require_admin

def delete_limit(limit_id):

    lim = CourseLimit.query.get_or_404(limit_id)

    db.session.delete(lim)

    db.session.commit()

    flash('Limits deleted.', 'warning')

    return redirect(url_for('admin.manage_limits'))



@admin_bp.route('/manage-timetable', methods=['GET', 'POST'])

@login_required

@require_admin

def manage_timetable():



    courses = Course.query.all()

    programmes = get_programme_choices()

    levels = get_level_choices()



    if request.method == 'POST':

        programme_name = request.form.get('programme_name')

        programme_level = request.form.get('programme_level')

        course_id = request.form.get('course_id')

        day = request.form.get('day')

        start_time = request.form.get('start_time')

        end_time = request.form.get('end_time')



        # Validate required fields

        if not all([programme_name, programme_level, course_id, day, start_time, end_time]):

            flash("All fields are required.", "error")

            return redirect(url_for('admin.manage_timetable'))



        # Validate time format and logic

        try:

            start_dt = datetime.strptime(start_time, "%H:%M").time()

            end_dt = datetime.strptime(end_time, "%H:%M").time()

            

            if start_dt >= end_dt:

                flash("Start time must be before end time.", "error")

                return redirect(url_for('admin.manage_timetable'))

        except ValueError:

            flash("Invalid time format.", "error")

            return redirect(url_for('admin.manage_timetable'))



        # Check for time conflicts

        existing = TimetableEntry.query.filter(

            TimetableEntry.programme_name == programme_name,

            TimetableEntry.programme_level == programme_level,

            TimetableEntry.day_of_week == day,

            TimetableEntry.start_time < end_dt,

            TimetableEntry.end_time > start_dt

        ).first()



        if existing:

            flash(f"Time slot conflicts with existing entry: {existing.course.name}", "error")

            return redirect(url_for('admin.manage_timetable'))



        # Create new entry

        new_entry = TimetableEntry(

            programme_name=programme_name,

            programme_level=programme_level,

            course_id=int(course_id),

            day_of_week=day,

            start_time=start_dt,

            end_time=end_dt

        )

        

        db.session.add(new_entry)

        db.session.commit()

        flash(f"Timetable entry added: {programme_name} Level {programme_level} - {day}", "success")

        return redirect(url_for('admin.manage_timetable'))



    timetable_entries = TimetableEntry.query.order_by(

        TimetableEntry.day_of_week, 

        TimetableEntry.start_time

    ).all()

    

    return render_template('admin/manage_timetable.html',

                           courses=courses,

                           programmes=programmes,

                           levels=levels,

                           timetable=timetable_entries)





@admin_bp.route('/edit-timetable-entry/<int:entry_id>', methods=['GET', 'POST'])

@login_required

@require_admin

def edit_timetable_entry(entry_id):

    entry = TimetableEntry.query.get_or_404(entry_id)

    courses = Course.query.all()

    programmes = get_programme_choices()

    levels = get_level_choices()



    if request.method == 'POST':

        entry.programme_name = request.form.get('programme_name')

        entry.programme_level = request.form.get('programme_level')

        entry.course_id = int(request.form.get('course_id'))

        entry.day_of_week = request.form.get('day')

        entry.start_time = datetime.strptime(request.form.get('start_time'), "%H:%M").time()

        entry.end_time = datetime.strptime(request.form.get('end_time'), "%H:%M").time()



        # Check for conflicts (excluding current entry)

        existing = TimetableEntry.query.filter(

            TimetableEntry.id != entry_id,

            TimetableEntry.programme_name == entry.programme_name,

            TimetableEntry.programme_level == entry.programme_level,

            TimetableEntry.day_of_week == entry.day_of_week,

            TimetableEntry.start_time < entry.end_time,

            TimetableEntry.end_time > entry.start_time

        ).first()



        if existing:

            flash(f"Time slot conflicts with: {existing.course.name}", "error")

            return redirect(url_for('admin.manage_timetable'))



        db.session.commit()

        flash("Timetable entry updated successfully.", "success")

        return redirect(url_for('admin.manage_timetable'))



    return render_template('admin/edit_timetable_entry.html',

                           entry=entry,

                           courses=courses,

                           programmes=programmes,

                           levels=levels)





@admin_bp.route('/delete-timetable-entry/<int:entry_id>', methods=['POST'])

@login_required

@require_admin

def delete_timetable_entry(entry_id):

    entry = TimetableEntry.query.get_or_404(entry_id)

    course_name = entry.course.name

    

    db.session.delete(entry)

    db.session.commit()

    

    flash(f"Timetable entry deleted: {course_name}", "success")

    return redirect(url_for('admin.manage_timetable'))



#--------------- Student Fees Management ---------------

from collections import defaultdict



from datetime import datetime



def create_fee_notification(fee_group, sender=None):

    """

    Create a notification for a fee assignment.



    Args:

        fee_group: ProgrammeFeeStructure object

        sender: User/Admin object creating the notification. Defaults to current_user.

    """

    if sender is None:

        sender = current_user



    # Determine sender_id and type

    if hasattr(sender, 'admin_id'):

        sender_id = sender.admin_id

        sender_type = 'admin'

    elif hasattr(sender, 'user_id'):

        sender_id = sender.user_id

        sender_type = 'user'

    else:

        sender_id = None

        sender_type = 'system'



    # Parse items if stored as JSON

    items = fee_group.items

    if isinstance(items, str):

        try:

            items = json.loads(items)

        except Exception:

            items = []



    # Build message text

    items_text = '\n'.join([f"  • {item['description']}: {item['amount']} GHS" for item in items])

    message = (

        f"A new fee has been assigned for your programme {fee_group.programme_name} Level {fee_group.programme_level}.\n\n"

        f"Academic Year: {fee_group.academic_year}\n"

        f"Semester: {fee_group.semester}\n"

        f"Description: {fee_group.description}\n"

        f"Total Amount: {fee_group.amount} GHS\n\n"

        f"Breakdown:\n{items_text}\n\n"

        f"Please check your Fees section for details."

    )



    # Create Notification object

    notification = Notification(

        type='fee',

        title=f'New Fee Assigned: {fee_group.description}',

        message=message,

        sender_id=sender_id,

        sender_type=sender_type,

        related_type='fee',

        related_id=fee_group.id,

        created_at=datetime.utcnow()

    )

    db.session.add(notification)

    db.session.flush()  # Get notification.id



    # Map class_level string to Students

    students = User.query.join(StudentProfile).filter(

        StudentProfile.current_programme == fee_group.programme_name,

        StudentProfile.programme_level == int(fee_group.programme_level)

    ).all()

    if not students:

        db.session.rollback()

        raise ValueError(f"No students found matching '{fee_group.programme_name}' and level '{fee_group.programme_level}'")



    # Create recipients for students

    for student in students:

        # Notify student

        db.session.add(NotificationRecipient(

            notification_id=notification.id,

            user_id=student.user_id,

            is_read=False

        ))



    db.session.commit()



# Replace these functions/routes in your admin_routes.py

@admin_bp.route('/assign-fees', methods=['GET', 'POST'])

@login_required

def assign_fees():

    if not current_user.is_admin:

        flash("Unauthorized", "danger")

        return redirect(url_for('main.index'))



    academic_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()

    CLASS_LEVELS = ['100', '200', '300', '400']  # Fixed format

    # Get current percentage settings
    current_year = str(datetime.now().year)
    current_settings = FeePercentageSettings.get_active_settings(current_year)



    if request.method == 'POST':

        # Handle percentage settings form
        if 'base_payment_percentage' in request.form:
            academic_year = request.form.get('academic_year')
            base_percentage = float(request.form.get('base_payment_percentage'))
            deadline_str = request.form.get('base_payment_deadline')
            allow_installments = 'allow_installments_after_base' in request.form
            description = request.form.get('description', '')
            
            if deadline_str:
                deadline = datetime.strptime(deadline_str, '%Y-%m-%d').date()
            else:
                flash("Base payment deadline is required.", "danger")
                return redirect(url_for('admin.assign_fees'))
            
            # Check if settings exist for this academic year
            existing_settings = FeePercentageSettings.get_active_settings(academic_year)
            
            if existing_settings:
                # Update existing settings
                existing_settings.base_payment_percentage = base_percentage
                existing_settings.base_payment_deadline = deadline
                existing_settings.allow_installments_after_base = allow_installments
                existing_settings.description = description
                existing_settings.updated_at = datetime.utcnow()
            else:
                # Create new settings
                new_settings = FeePercentageSettings(
                    base_payment_percentage=base_percentage,
                    base_payment_deadline=deadline,
                    academic_year=academic_year,
                    allow_installments_after_base=allow_installments,
                    description=description
                )
                db.session.add(new_settings)
            
            db.session.commit()
            flash("✓ Fee percentage settings saved successfully!", "success")
            return redirect(url_for('admin.assign_fees'))
        
        # Handle regular fee assignment form
        programme_name = request.form.get('programme_name')

        programme_level = request.form.get('class_level')  # ✅ rename

        study_format = request.form.get('study_format') or 'Regular'

        academic_year_id = request.form.get('academic_year')

        semester = request.form.get('semester')

        group_title = request.form.get('group_title') or 'Default'



        if not programme_name or not programme_level or not academic_year_id or not semester:

            flash("Missing required fields.", "danger")

            return redirect(url_for('admin.assign_fees'))



        # Format as single year only

        academic_year_obj = AcademicYear.query.get(academic_year_id)

        academic_year_str = str(academic_year_obj.start_date.year) if academic_year_obj else str(datetime.now().year)



        descriptions = request.form.getlist('description[]')

        amounts = request.form.getlist('amount[]')

        items = []

        total = 0.0

        for desc, amt in zip(descriptions, amounts):

            amt_f = float(amt or 0)

            items.append({'description': desc.strip(), 'amount': round(amt_f, 2)})

            total += amt_f



        if not items:

            flash("Add at least one fee item.", "danger")

            return redirect(url_for('admin.assign_fees'))



        existing = ProgrammeFeeStructure.query.filter_by(

            programme_name=programme_name,

            programme_level=programme_level,  # ✅ fix

            study_format=study_format,

            academic_year=academic_year_str,

            semester=semester,

            description=group_title

        ).first()



        if existing:

            flash(

                f"A fee group already exists for {programme_name} ({programme_level}) "

                f"{academic_year_str} {semester}.",

                "warning"

            )

            return redirect(url_for('admin.assign_fees'))



        new_group = ProgrammeFeeStructure(

            programme_name=programme_name,

            programme_level=programme_level,  # ✅ fix

            study_format=study_format,

            academic_year=academic_year_str,

            semester=semester,

            description=group_title,

            amount=round(total, 2),

            items=json.dumps(items)

        )



        db.session.add(new_group)

        db.session.commit()

        

        # Notify students about fee assignment

        try:

            notify_fee_assigned(new_group, send_email=True)

        except Exception as e:

            current_app.logger.warning(f"Fee notification failed: {e}")

        

        # Notify based on level

        if programme_level == '100':

            flash("✓ Level 100 (Freshers) fees created - will auto-assign upon admission approval.", "success")

        else:

            flash(f"✓ Level {programme_level} (Continuing Students) fees created - assign manually to students.", "success")

        

        return redirect(url_for('admin.assign_fees'))



    # Separate groups by level for display

    groups = ProgrammeFeeStructure.query.order_by(

        ProgrammeFeeStructure.programme_name,

        ProgrammeFeeStructure.programme_level,  # ✅ fix

        ProgrammeFeeStructure.academic_year,

        ProgrammeFeeStructure.semester,

        ProgrammeFeeStructure.created_at.desc()

    ).all()



    return render_template(
        'admin/assign_fees.html',
        groups=groups,
        CERTIFICATE_PROGRAMMES=CERTIFICATE_PROGRAMMES,
        DIPLOMA_PROGRAMMES=DIPLOMA_PROGRAMMES,
        CLASS_LEVELS=CLASS_LEVELS,
        STUDY_FORMATS=STUDY_FORMATS,
        academic_years=academic_years,
        current_settings=current_settings
    )





def assign_fees_to_continuing_student(student_user_id, fee_structure_id):

    """

    Manually assign fees to Level 200+ student.

    

    Args:

        student_user_id: Student's user_id (e.g., "STD001")

        fee_structure_id: ID of fee structure to assign

    

    Returns:

        dict: {'success': bool, 'message': str}

    """

    try:

        student = User.query.filter_by(user_id=student_user_id).first()

        if not student:

            return {'success': False, 'message': f"Student {student_user_id} not found"}

        

        fee_struct = ProgrammeFeeStructure.query.get(fee_structure_id)

        if not fee_struct:

            return {'success': False, 'message': "Fee structure not found"}

        

        existing = StudentFeeBalance.query.filter_by(

            student_id=student_user_id,

            fee_structure_id=fee_structure_id

        ).first()

        

        if existing:

            return {'success': False, 'message': "Fees already assigned"}

        

        balance = StudentFeeBalance(

            student_id=student_user_id,

            fee_structure_id=fee_structure_id,

            programme_name=fee_struct.programme_name,

            programme_level=fee_struct.programme_level,

            academic_year=fee_struct.academic_year,

            semester=fee_struct.semester,

            amount_due=fee_struct.amount,

            amount_paid=0.0,

            is_paid=False

        )

        

        db.session.add(balance)

        db.session.commit()

        

        logger.info(f"✓ Fees assigned to {student_user_id}")

        return {'success': True, 'message': f"Assigned GHS {fee_struct.amount}"}

    

    except Exception as e:

        db.session.rollback()

        logger.exception(f"Error: {e}")

        return {'success': False, 'message': str(e)}





def bulk_assign_fees(programme_name, level, study_format, fee_structure_id):

    """

    Assign same fees to all students in a level.

    

    Args:

        programme_name: Programme name

        level: Level (100, 200, 300, 400)

        study_format: Study format (Regular, Weekend, Online)

        fee_structure_id: Fee structure ID

    

    Returns:

        dict: {'assigned': int, 'skipped': int, 'errors': list}

    """

    try:

        fee_struct = ProgrammeFeeStructure.query.get(fee_structure_id)

        if not fee_struct:

            return {'assigned': 0, 'skipped': 0, 'errors': ['Fee structure not found']}

        

        students = StudentProfile.query.filter_by(

            current_programme=programme_name,

            programme_level=level,

            study_format=study_format

        ).all()

        

        assigned = 0

        skipped = 0

        errors = []

        level_str = str(level)

        

        for profile in students:

            try:

                existing = StudentFeeBalance.query.filter_by(

                    student_id=profile.user_id,

                    fee_structure_id=fee_structure_id

                ).first()

                

                if existing:

                    skipped += 1

                    continue

                

                balance = StudentFeeBalance(

                    student_id=profile.user_id,

                    fee_structure_id=fee_structure_id,

                    programme_name=programme_name,

                    programme_level=level_str,

                    academic_year=fee_struct.academic_year,

                    semester=fee_struct.semester,

                    amount_due=fee_struct.amount,

                    amount_paid=0.0,

                    is_paid=False

                )

                db.session.add(balance)

                assigned += 1

            except Exception as e:

                errors.append(str(e))

        

        db.session.commit()

        logger.info(f"Bulk: assigned {assigned}, skipped {skipped}")

        return {'assigned': assigned, 'skipped': skipped, 'errors': errors}

    

    except Exception as e:

        db.session.rollback()

        logger.exception(f"Bulk assignment error: {e}")

        return {'assigned': 0, 'skipped': 0, 'errors': [str(e)]}



@admin_bp.route('/edit-fee-group/<int:group_id>', methods=['GET', 'POST'])
@login_required
def edit_fee_group(group_id):
    if not current_user.is_admin:
        flash("Unauthorized", "danger")
        return redirect(url_for('main.index'))
    group = ProgrammeFeeStructure.query.get_or_404(group_id)
    academic_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    CLASS_LEVELS = ['100 Level', '200 Level', '300 Level', '400 Level']
    
    # Get percentage settings for the fee group's academic year
    group_academic_year = group.academic_year if group else str(datetime.now().year)
    print(f"DEBUG: Fee group academic_year: {group_academic_year}")
    current_settings = FeePercentageSettings.get_active_settings(group_academic_year)
    print(f"DEBUG: Retrieved current_settings: {current_settings}")
    if current_settings:
        print(f"DEBUG: Settings details - percentage: {current_settings.base_payment_percentage}, deadline: {current_settings.base_payment_deadline}, installments: {current_settings.allow_installments_after_base}")
    if request.method == 'POST':
        # Debug: Print all form data
        print(f"DEBUG: Form data received: {dict(request.form)}")
        print(f"DEBUG: save_percentage_settings value: {request.form.get('save_percentage_settings')}")
        
        # Handle percentage settings form
        if request.form.get('save_percentage_settings'):
            academic_year = request.form.get('academic_year')
            base_percentage = float(request.form.get('base_payment_percentage'))
            deadline_str = request.form.get('base_payment_deadline')
            allow_installments = 'allow_installments_after_base' in request.form
            description = request.form.get('description', '')
            if deadline_str:
                deadline = datetime.strptime(deadline_str, '%Y-%m-%d').date()
            else:
                flash("Base payment deadline is required.", "danger")
                return redirect(url_for('admin.edit_fee_group', group_id=group_id))
            # Check if settings exist for this academic year
            existing_settings = FeePercentageSettings.get_active_settings(academic_year)
            print(f"DEBUG: Existing settings found: {existing_settings}")
            if existing_settings:
                # Update existing settings
                print(f"DEBUG: Updating existing settings with: percentage={base_percentage}, deadline={deadline}")
                existing_settings.base_payment_percentage = base_percentage
                existing_settings.base_payment_deadline = deadline
                existing_settings.allow_installments_after_base = allow_installments
                existing_settings.description = description
                existing_settings.updated_at = datetime.utcnow()
            else:
                # Create new settings
                print(f"DEBUG: Creating new settings with: academic_year={academic_year}, percentage={base_percentage}, deadline={deadline}")
                new_settings = FeePercentageSettings(
                    base_payment_percentage=base_percentage,
                    base_payment_deadline=deadline,
                    academic_year=academic_year,
                    allow_installments_after_base=allow_installments,
                    description=description
                )
                db.session.add(new_settings)

            print(f"DEBUG: Committing to database...")
            db.session.commit()
            print(f"DEBUG: Database commit completed")
            flash("✓ Fee percentage settings saved successfully!", "success")
            return redirect(url_for('admin.edit_fee_group', group_id=group_id))

        # If this is the main fee group form (not percentage settings), handle fee group update
        if not request.form.get('save_percentage_settings'):
            group.programme_name = request.form.get('programme_name')
            group.programme_level = request.form.get('programme_level')  # Fix: use programme_level not class_level
            group.study_format = request.form.get('study_format')

            # Keep single year format
            academic_year_obj = AcademicYear.query.get(request.form.get('academic_year'))
            group.academic_year = str(academic_year_obj.start_date.year) if academic_year_obj else str(datetime.now().year)

            group.semester = request.form.get('semester')
            group.description = request.form.get('group_title') or group.description

            descriptions = request.form.getlist('description[]')
            amounts = request.form.getlist('amount[]')
            items = []
            total = 0.0

            for desc, amt in zip(descriptions, amounts):
                amt_f = float(amt or 0)
                items.append({'description': desc.strip(), 'amount': round(amt_f, 2)})
                total += amt_f

            group.items = json.dumps(items)
            group.amount = round(total, 2)

            db.session.commit()

            flash("Fee group updated successfully.", "success")
            return redirect(url_for('admin.assign_fees'))

    group_items = json.loads(group.items or '[]')
    return render_template(
        'admin/edit_fee_group.html',
        group=group,
        group_items=group_items,
        academic_years=academic_years,
        CLASS_LEVELS=CLASS_LEVELS,
        STUDY_FORMATS=STUDY_FORMATS,
        CERTIFICATE_PROGRAMMES=CERTIFICATE_PROGRAMMES,
        DIPLOMA_PROGRAMMES=DIPLOMA_PROGRAMMES,
        current_settings=current_settings
    )

@admin_bp.route('/delete-fee/<int:fee_id>', methods=['POST'])
@login_required
def delete_fee(fee_id):
    group = ProgrammeFeeStructure.query.get_or_404(fee_id)
    
    try:
        # Check for related student fee balance records
        student_balances = StudentFeeBalance.query.filter_by(fee_structure_id=fee_id).all()
        
        if student_balances:
            # Delete related student fee balance records
            balance_count = len(student_balances)
            for balance in student_balances:
                db.session.delete(balance)
            
            # Also delete any related transactions
            from models import StudentFeeTransaction
            transactions = StudentFeeTransaction.query.filter_by(fee_structure_id=fee_id).all()
            for txn in transactions:
                db.session.delete(txn)
            
            # Now delete the fee group
            db.session.delete(group)
            db.session.commit()
            flash(f"Fee group and {balance_count} related student balance records deleted successfully.", "success")
        else:
            # No related records, safe to delete
            db.session.delete(group)
            db.session.commit()
            flash("Fee group deleted successfully.", "success")
            
    except Exception as e:
        db.session.rollback()
        if "foreign key constraint" in str(e).lower():
            flash("Cannot delete fee group: It is referenced by student fee records. Please delete student records first or contact administrator.", "danger")
        else:
            flash(f"Error deleting fee group: {str(e)}", "danger")
            
    return redirect(url_for('admin.assign_fees'))


@admin_bp.route('/admin/fees/group/<int:group_id>/delete', methods=['POST'])
@login_required
def delete_fee_group(group_id):
    group = ProgrammeFeeStructure.query.get_or_404(group_id)

    try:
        # Check for related student fee balance records
        student_balances = StudentFeeBalance.query.filter_by(fee_structure_id=group_id).all()
        
        if student_balances:
            # Option 1: Delete related student fee balance records (clean approach)
            balance_count = len(student_balances)
            for balance in student_balances:
                db.session.delete(balance)
            
            # Also delete any related transactions
            from models import StudentFeeTransaction
            transactions = StudentFeeTransaction.query.filter_by(fee_structure_id=group_id).all()
            for txn in transactions:
                db.session.delete(txn)
            
            # Now delete the fee group
            db.session.delete(group)
            db.session.commit()
            flash(f"Fee group and {balance_count} related student balance records deleted successfully.", "success")
        else:
            # No related records, safe to delete
            db.session.delete(group)
            db.session.commit()
            flash("Fee group deleted successfully.", "success")

    except Exception as e:
        db.session.rollback()
        if "foreign key constraint" in str(e).lower():
            flash("Cannot delete fee group: It is referenced by student fee records. Please delete student records first or contact administrator.", "danger")
        else:
            flash(f"Error deleting fee group: {str(e)}", "danger")

    return redirect(url_for('admin.assign_fees'))


@admin_bp.route('/mark_fee_paid/<int:fee_id>', methods=['POST'])
@login_required
def mark_fee_paid(fee_id):
    fee = StudentFeeBalance.query.get_or_404(fee_id)

    if fee.is_paid: 
        flash("This fee is already marked as paid.", "info")
    else:
        fee.is_paid = True
        fee.paid_on = datetime.utcnow()
        db.session.commit()
        flash("Fee marked as paid successfully.", "success")

    return redirect(url_for('admin.assign_fees'))


@admin_bp.route('/review-payments')
@login_required
@require_finance_admin
def review_payments():
    """Review pending payments for approval or rejection"""
    try:
        status_filter = request.args.get('status', 'pending')
        query = StudentFeeTransaction.query

        if status_filter in ['pending', 'approved', 'rejected']:
            query = query.filter_by(is_approved=(status_filter == 'approved'))

        transactions = query.order_by(StudentFeeTransaction.timestamp.desc()).all()
        
        return render_template(
            'admin/review_payments.html',
            transactions=transactions,
            status_filter=status_filter
        )
    except Exception as e:
        logger.exception(f"Error reviewing payments: {e}")
        abort(500)


from sqlalchemy.exc import IntegrityError

@admin_bp.route('/approve-payment/<int:txn_id>', methods=['POST'])
@login_required
def approve_payment(txn_id):
    """Approve a fee payment and update student balance (robust tertiary version)."""
    txn = StudentFeeTransaction.query.get_or_404(txn_id)

    if getattr(txn, 'is_approved', False):
        flash("Already approved", "warning")
        return redirect(url_for('admin.review_payments'))

    try:
        # mark transaction approved
        txn.is_approved = True
        txn.reviewed_by_admin_id = current_user.id
        if hasattr(txn, 'reviewed_at'):
            txn.reviewed_at = datetime.utcnow()

        # Resolve student and canonical student_user_id (string STDxxx)
        student = getattr(txn, 'student', None)
        if not student:
            # txn.student_id might be integer id — try to resolve
            student = User.query.get(txn.student_id)
        if not student:
            flash("Student record not found for this transaction.", "danger")
            db.session.rollback()
            return redirect(url_for('admin.review_payments'))

        student_user_id = getattr(student, 'user_id', None) or str(getattr(student, 'id', None))

        # Try to find an existing balance by student + year + semester first (best effort)
        balance = StudentFeeBalance.query.filter_by(
            student_id=student_user_id,
            academic_year=txn.academic_year,
            semester=txn.semester
        ).first()

        fee_structure = None
        if not balance:
            # No existing balance for that year/semester — try to find matching ProgrammeFeeStructure
            profile = getattr(student, 'student_profile', None)
            programme_name = getattr(profile, 'current_programme', None) if profile else None
            programme_level = getattr(profile, 'programme_level', None) if profile else None
            study_format = getattr(profile, 'study_format', None) if profile else None

            def normalize_level(level):
                if level is None:
                    return None
                try:
                    return str(int(level))
                except Exception:
                    return str(level)

            programme_level_str = normalize_level(programme_level)

            if programme_name and programme_level_str:
                # try the standard column names
                fee_structure = ProgrammeFeeStructure.query.filter_by(
                    programme_name=programme_name,
                    programme_level=programme_level_str,
                    study_format=study_format or 'Regular',
                    academic_year=txn.academic_year,
                    semester=txn.semester
                ).first()

            # If found, try to reuse or create balance tied to that fee_structure
            if fee_structure:
                # prefer to find existing balance by (student_id, fee_structure_id)
                balance = StudentFeeBalance.query.filter_by(
                    student_id=student_user_id,
                    fee_structure_id=fee_structure.id
                ).first()

                if not balance:
                    # create new balance for the fee_structure
                    balance = StudentFeeBalance(
                        student_id=student_user_id,
                        fee_structure_id=fee_structure.id,
                        programme_name=fee_structure.programme_name,
                        programme_level=fee_structure.programme_level,
                        study_format=fee_structure.study_format if hasattr(fee_structure, 'study_format') else 'Regular',
                        academic_year=txn.academic_year,
                        semester=txn.semester,
                        amount_due=fee_structure.amount or 0.0,
                        amount_paid=0.0,
                        is_paid=False
                    )
                    db.session.add(balance)
                    try:
                        db.session.flush()  # might raise IntegrityError if another process created the same row
                    except IntegrityError:
                        db.session.rollback()
                        # re-query the existing balance (concurrent insert raced us)
                        balance = StudentFeeBalance.query.filter_by(
                            student_id=student_user_id,
                            fee_structure_id=fee_structure.id
                        ).first()
                        if not balance:
                            # Very odd — re-raise after logging
                            logging.exception("Failed to create StudentFeeBalance and couldn't find existing one.")
                            flash("System error creating student balance; contact admin.", "danger")
                            return redirect(url_for('admin.review_payments'))
            else:
                # No fee structure and no existing balance found.
                # We will not create a new (unlinked) balance because fee_structure_id is required.
                logging.warning("No fee structure found for student %s %s %s; skipping balance creation.",
                                student_user_id, txn.academic_year, txn.semester)
                flash("Payment approved but no matching fee structure found; balance not created.", "warning")

        # At this point, if we have a balance, update it with txn.amount
        if balance:
            if balance.amount_paid is None:
                balance.amount_paid = 0.0
            balance.amount_paid = (balance.amount_paid or 0.0) + (txn.amount or 0.0)

            # If fully paid, mark is_paid and set paid_on if column exists
            try:
                if (balance.amount_due or 0.0) > 0 and (balance.amount_paid or 0.0) >= (balance.amount_due or 0.0):
                    balance.is_paid = True
                    if hasattr(balance, 'paid_on'):
                        balance.paid_on = datetime.utcnow()
            except Exception:
                # ignore model differences
                pass

        # Optionally: generate receipt (wrapped so errors won't block)
        try:
            receipt_filename = generate_receipt(txn, student)
            if receipt_filename and hasattr(txn, 'receipt_filename'):
                txn.receipt_filename = receipt_filename
        except Exception:
            logging.exception("Receipt generation failed; continuing.")
        db.session.commit()
        flash("Payment approved, balance updated, and receipt generated.", "success")

    except IntegrityError as ie:
        db.session.rollback()
        logging.exception("IntegrityError while approving payment")
        # Try to recover: find existing balance and update
        try:
            student_user_id = getattr(student, 'user_id', None) or str(getattr(student, 'id', None))
            if fee_structure:
                balance = StudentFeeBalance.query.filter_by(
                    student_id=student_user_id,
                    fee_structure_id=fee_structure.id
                ).first()
            else:
                balance = StudentFeeBalance.query.filter_by(
                    student_id=student_user_id,
                    academic_year=txn.academic_year,
                    semester=txn.semester
                ).first()

            if balance:
                if balance.amount_paid is None:
                    balance.amount_paid = 0.0
                balance.amount_paid = (balance.amount_paid or 0.0) + (txn.amount or 0.0)
                if (balance.amount_due or 0.0) > 0 and (balance.amount_paid or 0.0) >= (balance.amount_due or 0.0):
                    balance.is_paid = True
                    if hasattr(balance, 'paid_on'):
                        balance.paid_on = datetime.utcnow()
                # attach receipt if possible
                try:
                    receipt_filename = generate_receipt(txn, student)
                    if receipt_filename and hasattr(txn, 'receipt_filename'):
                        txn.receipt_filename = receipt_filename
                except Exception:
                    logging.exception("Receipt generation failed in recovery.")
                db.session.commit()
                flash("Payment approved (recovered from race) and balance updated.", "success")
            else:
                flash("Payment approved but failed to create/update balance due to a concurrency error. Contact admin.", "warning")
        except Exception:
            db.session.rollback()
            logging.exception("Recovery after IntegrityError failed")
            flash("System error while approving payment (post-recovery).", "danger")

    except Exception as e:
        db.session.rollback()
        logging.exception("Approval process failed")
        flash(f"System error while approving payment: {e}", "danger")

    return redirect(url_for('admin.review_payments'))


@admin_bp.route('/reject-payment/<int:txn_id>', methods=['POST'])
@login_required
def reject_payment(txn_id):
    """Reject a pending fee payment, remove associated StudentFeeBalance, and delete transaction."""
    txn = StudentFeeTransaction.query.get_or_404(txn_id)

    if getattr(txn, 'is_approved', False):
        flash("Cannot reject — transaction already approved.", "warning")
        return redirect(url_for('admin.review_payments'))

    try:
        # Get the student's string ID (like 'STD001') from the student relationship
        student_str_id = txn.student.user_id

        # Delete associated fee balances for this student, year, and semester
        balances = StudentFeeBalance.query.filter_by(
            student_id=student_str_id,
            academic_year=txn.academic_year,
            semester=txn.semester
        ).all()

        for balance in balances:
            db.session.delete(balance)

        # Delete the transaction itself
        db.session.delete(txn)

        db.session.commit()

        flash("Payment rejected, associated balances removed, and transaction deleted.", "info")

    except Exception as e:
        db.session.rollback()
        logging.exception("Reject process failed")
        flash(f"System error while rejecting payment: {e}", "danger")

    return redirect(url_for('admin.review_payments'))


@admin_bp.route('/password-reset-requests')
@login_required
def password_reset_requests_view():
    # Expire old requests
    now = datetime.utcnow()

    expired_requests = PasswordResetRequest.query.join(PasswordResetToken).filter(
        PasswordResetRequest.status.in_(['emailed', 'email_failed']),
        PasswordResetToken.expires_at < now
    ).all()
    for req in expired_requests:
        req.status = 'expired'
    if expired_requests:
        db.session.commit()

    requests = PasswordResetRequest.query.order_by(PasswordResetRequest.requested_at.desc()).all()
    return render_template('admin/password_reset_requests.html', requests=requests)


def retry_failed_emails():
    failed_requests = PasswordResetRequest.query.filter_by(status='email_failed').all()
    for req in failed_requests:
        token = req.tokens[-1].token_hash  # last token
        try:
            send_password_reset_email(req.user, token)
            req.status = 'emailed'
            req.email_sent_at = datetime.utcnow()
        except Exception:
            continue
    db.session.commit()


@admin_bp.route('/password-reset/<int:request_id>', methods=['POST'])
@login_required
def reset_user_password(request_id):
    req = PasswordResetRequest.query.get_or_404(request_id)
    user = User.query.filter_by(user_id=req.user_id).first()

    if not user:
        flash('User not found.', 'danger')
        req.status = 'failed'
        db.session.commit()
        return redirect(url_for('admin.password_reset_requests_view'))

    temp_password = secrets.token_urlsafe(8)
    user.set_password(temp_password)
    db.session.commit()

    req.status = 'completed'
    req.completed_at = datetime.utcnow()
    db.session.commit()

    try:
        send_temporary_password_email(user, temp_password)
        flash(f'Password for {user.user_id} has been reset and emailed.', 'success')
    except Exception as e:
        current_app.logger.exception(f"Failed to send email: {e}")
        flash(f'Password reset succeeded, but email failed for {user.user_id}.', 'warning')

    return redirect(url_for('admin.password_reset_requests_view'))


@admin_bp.route('/teacher-assessment')
@login_required
def teacher_assessment_admin_home():
    if not current_user.is_admin:
        abort(403)

    active_period = TeacherAssessmentPeriod.query.filter_by(is_active=True).first()

    return render_template(
        'admin/teacher_assessment_home.html',

        active_period=active_period
    )

@admin_bp.route('/teacher-assessment/questions')
@login_required
def teacher_assessment_questions():
    if not current_user.is_admin:
        abort(403)

    questions = TeacherAssessmentQuestion.query.order_by(
        TeacherAssessmentQuestion.category,
        TeacherAssessmentQuestion.id
    ).all()

    return render_template(
        'admin/teacher_assessment_questions.html',
        questions=questions
    )

@admin_bp.route('/teacher-assessment/questions/add', methods=['GET', 'POST'])
@login_required
def add_teacher_assessment_question():
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        question = request.form.get('question')
        category = request.form.get('category')

        if not question or not category:
            flash("All fields are required.", "danger")
            return redirect(request.url)

        q = TeacherAssessmentQuestion(
            question=question,
            category=category
        )
        db.session.add(q)
        db.session.commit()

        flash("Question added successfully.", "success")
        return redirect(url_for('admin.teacher_assessment_questions'))

    return render_template('admin/teacher_assessment_question_form.html')


@admin_bp.route('/teacher-assessment/questions/<int:qid>/edit', methods=['GET', 'POST'])
@login_required
def edit_teacher_assessment_question(qid):
    if not current_user.is_admin:
        abort(403)

    q = TeacherAssessmentQuestion.query.get_or_404(qid)

    if request.method == 'POST':
        q.question = request.form.get('question')
        q.category = request.form.get('category')
        q.is_active = bool(request.form.get('is_active'))

        db.session.commit()
        flash("Question updated.", "success")
        return redirect(url_for('admin.teacher_assessment_questions'))

    return render_template(
        'admin/teacher_assessment_question_form.html',
        q=q
    )


@admin_bp.route('/teacher-assessment/questions/<int:qid>/delete', methods=['POST'])
@login_required
def delete_teacher_assessment_question(qid):
    if not current_user.is_admin:
        abort(403)

    q = TeacherAssessmentQuestion.query.get_or_404(qid)

    db.session.delete(q)
    db.session.commit()

    flash("Question deleted.", "success")
    return redirect(url_for('admin.teacher_assessment_questions'))


@admin_bp.route('/teacher-assessment/periods')
@login_required
def assessment_periods():
    if not current_user.is_admin:
        abort(403)

    periods = TeacherAssessmentPeriod.query.order_by(
        TeacherAssessmentPeriod.created_at.desc()
    ).all()
    
    # Calculate statistics for each period
    for period in periods:
        assessments = TeacherAssessment.query.filter_by(period_id=period.id).all()
        period.assessment_count = len(assessments)
        period.student_count = len(set(a.student_id for a in assessments))
    
    return render_template(
        'admin/teacher_assessment_periods.html',
        periods=periods
    )


@admin_bp.route('/teacher-assessment/periods/add', methods=['GET', 'POST'])
@login_required
def add_assessment_period():
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        academic_year = request.form.get('academic_year')
        semester = request.form.get('semester')
        start_date = datetime.strptime(request.form.get('start_date'), "%Y-%m-%d").date()
        end_date = datetime.strptime(request.form.get('end_date'), "%Y-%m-%d").date()
        activate = request.form.get('activate')

        if not all([academic_year, semester, start_date, end_date]):
            flash("All fields are required.", "danger")
            return redirect(request.url)

        if activate:
            # Deactivate all other periods
            TeacherAssessmentPeriod.query.update({TeacherAssessmentPeriod.is_active: False})

        period = TeacherAssessmentPeriod(
            academic_year=academic_year,
            semester=semester,
            start_date=start_date,
            end_date=end_date,
            is_active=bool(activate)
        )

        db.session.add(period)
        db.session.commit()

        flash("Assessment period created successfully.", "success")
        return redirect(url_for('admin.assessment_periods'))

    return render_template('admin/teacher_assessment_period_form.html')


@admin_bp.route('/teacher-assessment/periods/<int:pid>/results')
@login_required
def assessment_period_results(pid):
    if not current_user.is_admin:
        abort(403)
    
    period = TeacherAssessmentPeriod.query.get_or_404(pid)
    
    # Get all assessments for this period
    assessments = TeacherAssessment.query.filter_by(period_id=pid).all()
    
    # Calculate statistics
    total_assessments = len(assessments)
    unique_students = len(set(a.student_id for a in assessments))
    unique_teachers = len(set(a.teacher_id for a in assessments))
    
    # Average ratings (assuming 1-5 scale)
    avg_rating = 0
    if assessments:
        # Calculate average from answers (you may need to adjust based on your rating system)
        ratings = []
        for assessment in assessments:
            answers = TeacherAssessmentAnswer.query.filter_by(assessment_id=assessment.id).all()
            for answer in answers:
                # Assuming answers have a score/rating field
                if hasattr(answer, 'score') and answer.score:
                    ratings.append(float(answer.score))
        avg_rating = sum(ratings) / len(ratings) if ratings else 0
    
    # Teacher performance breakdown
    teacher_stats = {}
    for assessment in assessments:
        teacher_id = assessment.teacher_id
        if teacher_id not in teacher_stats:
            teacher_stats[teacher_id] = {
                'name': assessment.teacher_name if hasattr(assessment, 'teacher_name') else f"Teacher {teacher_id}",
                'assessments': 0,
                'avg_rating': 0,
                'courses': set()
            }
        
        teacher_stats[teacher_id]['assessments'] += 1
        teacher_stats[teacher_id]['courses'].add(assessment.course_name)
        
        # Calculate individual teacher rating
        answers = TeacherAssessmentAnswer.query.filter_by(assessment_id=assessment.id).all()
        teacher_ratings = []
        for answer in answers:
            if hasattr(answer, 'score') and answer.score:
                teacher_ratings.append(float(answer.score))
        teacher_stats[teacher_id]['avg_rating'] = sum(teacher_ratings) / len(teacher_ratings) if teacher_ratings else 0
    
    # Convert sets to lists for JSON serialization
    for teacher_id in teacher_stats:
        teacher_stats[teacher_id]['courses'] = list(teacher_stats[teacher_id]['courses'])
    
    # Course breakdown
    course_stats = {}
    for assessment in assessments:
        course = assessment.course_name
        if course not in course_stats:
            course_stats[course] = {'count': 0, 'avg_rating': 0}
        course_stats[course]['count'] += 1
    
    return render_template(
        'admin/assessment_period_results.html',
        period=period,
        assessments=assessments,
        total_assessments=total_assessments,
        unique_students=unique_students,
        unique_teachers=unique_teachers,
        avg_rating=avg_rating,
        teacher_stats=teacher_stats,
        course_stats=course_stats
    )


@admin_bp.route('/teacher-assessment/periods/<int:pid>/toggle', methods=['POST'])
@login_required
def toggle_assessment_period(pid):
    if not current_user.is_admin:
        abort(403)

    period = TeacherAssessmentPeriod.query.get_or_404(pid)

    if not period.is_active:
        # Ensure only one active period
        TeacherAssessmentPeriod.query.update({TeacherAssessmentPeriod.is_active: False})
        period.is_active = True
        flash("Assessment period activated.", "success")
    else:
        period.is_active = False
        flash("Assessment period closed.", "warning")

    db.session.commit()
    return redirect(url_for('admin.assessment_periods'))


# =====================================================

# Admin - Manage Admissions

# =====================================================

@admin_bp.route('/admissions')
@login_required
@require_admissions_admin
def manage_admissions():
    status = request.args.get('status')

    query = Application.query
    if status:
        query = query.filter_by(status=status)

    applications = query.order_by(Application.submitted_at.desc()).all()

    stats = {
        'total': Application.query.count(),
        'submitted': Application.query.filter_by(status='submitted').count(),
        'approved': Application.query.filter_by(status='approved').count(),
        'rejected': Application.query.filter_by(status='rejected').count()
    }

    return render_template(
        'admin/manage_admissions.html',
        applications=applications,
        status=status,
        stats=stats
    )


@admin_bp.route('/admissions/<int:app_id>')
@login_required
@require_admissions_admin
def view_application(app_id):
    application = Application.query.get_or_404(app_id)

    return render_template(
        'admin/view_application.html',
        application=application,
        documents=application.documents,
        results=application.exam_results
    )


@admin_bp.route('/admissions/<int:app_id>/update-status/<string:new_status>', methods=['POST'])
@login_required
@require_admissions_admin
def update_application_status(app_id, new_status):
    """Update application status and handle approval/rejection (tertiary version)"""
    application = Application.query.get_or_404(app_id)

    if new_status not in ['draft', 'submitted', 'approved', 'rejected']:
        flash('Invalid status.', 'danger')
        return redirect(url_for('admin.manage_admissions'))

    application.status = new_status

    try:
        # ================= APPROVED =================
        if new_status == 'approved':
            applicant_email = application.email or (
                application.applicant.email if getattr(application, 'applicant', None) else None
            )

            if not applicant_email:
                flash("Applicant email missing.", "danger")
                return redirect(url_for('admin.manage_admissions'))

            existing_user = User.query.filter_by(email=applicant_email).first()
            if existing_user:
                flash("Student account already exists for this applicant.", "info")
                db.session.commit()
                return redirect(url_for('admin.manage_admissions'))

            # -------- CREATE STUDENT ACCOUNT (TERTIARY) --------
            # Generate username: first name + middle initials + last name
            middle_initials = ''.join([word[0] for word in (application.other_names or '').split()])

            username = generate_unique_username(
                application.first_name or '',
                middle_initials,
                application.surname or '',
                'student'
            )

            prefix = 'STD'
            count = User.query.filter_by(role='student').count() + 1
            while User.query.filter_by(user_id=f"{prefix}{count:03d}").first():
                count += 1
            student_id = f"{prefix}{count:03d}"

            temp_password = uuid.uuid4().hex[:8]

            photo_doc = next((doc for doc in application.documents if doc.document_type.lower() == 'photo'), None)

            # Use uploaded photo filename if exists, else default filename
            # Store only the filename (templates expect just the filename)
            if photo_doc:
                profile_picture_filename = os.path.basename(photo_doc.file_path)
            else:
                profile_picture_filename = 'default_avatar.png'
            profile_picture_path = profile_picture_filename

            new_user = User(
                user_id=student_id,
                username=username,
                email=applicant_email,
                first_name=application.first_name,
                middle_name=application.other_names,
                last_name=application.surname,
                role='student',
                profile_picture=profile_picture_path
            )

            new_user.set_password(temp_password)
            db.session.add(new_user)
            db.session.flush()

            # -------- CREATE STUDENT PROFILE (TERTIARY) --------
            # Map application's first_choice to tertiary programme
            # Generate proper index number
            generated_index = generate_index_number(
                programme_name=application.admitted_programme or application.first_choice,
                admission_date=datetime.utcnow().date()
            )

            student_profile = StudentProfile(
                user_id=new_user.user_id,
                dob=application.dob,
                gender=application.gender,
                nationality=application.nationality,
                address=application.postal_address,
                phone=application.phone,
                email=applicant_email,

                guardian_name=application.guardian_name,
                guardian_relation=application.guardian_relation,
                guardian_phone=application.guardian_phone,
                guardian_email=application.guardian_email,
                guardian_address=application.guardian_address,

                current_programme=application.admitted_programme or application.first_choice,
                programme_level=100,
                study_format=application.admitted_stream or 'Regular',

                # ✅ NEW SYSTEM
                index_number=generated_index,

                academic_status='Active',
                admission_date=datetime.utcnow().date(),
                academic_year=application.admitted_academic_year or f"{datetime.utcnow().year}/{datetime.utcnow().year + 1}",
                semester=application.admitted_semester or '1'
            )

            db.session.add(student_profile)
            db.session.flush()

            # ========== ✅ AUTO ASSIGN FEES (TERTIARY) ==========
            fees_assigned = assign_fees_to_student_tertiary(new_user, student_profile)

            db.session.commit()

            flash(f"Student account created! Username: {username}", "success")
            flash(f"✓ Index Number: {student_profile.index_number}", "info")

            if fees_assigned:
                flash("✓ Fees automatically assigned to student.", "info")
            else:
                flash("⚠ Student created but NO matching fee structure found.", "warning")

            # Prepare fees information for email
            fees_info = None
            programme_name = application.admitted_programme or application.first_choice
            fee_structures = ProgrammeFeeStructure.query.filter_by(
                programme_name=programme_name,
                programme_level='100',
                study_format=application.admitted_stream or 'Regular'
            ).all()
            
            if fee_structures:
                fees_list = []
                for fee_structure in fee_structures:
                    # Use items_list property to get JSON-stored fee components
                    items = fee_structure.items_list if hasattr(fee_structure, 'items_list') else []
                    if items:
                        # If items exist, use them (they contain the actual fee details)
                        for item in items:
                            fees_list.append({
                                'description': item.get('name', item.get('description', 'Fee')),
                                'amount': item.get('amount', 0)
                            })
                    else:
                        # Fallback to description and amount if no items
                        if fee_structure.description != 'Default':
                            fees_list.append({
                                'description': fee_structure.description,
                                'amount': fee_structure.amount
                            })

                if fees_list:
                    fees_info = {
                        'programme_name': programme_name,
                        'fees': fees_list
                    }

            # Send credentials with fees info
            send_approval_credentials_email(application, username, student_id, temp_password, fees_info)

        # ================= REJECTED =================
        elif new_status == 'rejected':
            applicant_email = application.email or (
                application.applicant.email if getattr(application, 'applicant', None) else None
            )

            if applicant_email:
                user = User.query.filter_by(email=applicant_email, role='student').first()
                if user:
                    # Also delete associated fees
                    StudentFeeBalance.query.filter_by(student_id=user.user_id).delete()
                    profile = StudentProfile.query.filter_by(user_id=user.user_id).first()
                    if profile:
                        db.session.delete(profile)
                    db.session.delete(user)

            db.session.commit()
            flash("Application rejected and student account removed.", "info")

        else:
            db.session.commit()
            flash(f'Application status updated to {new_status}.', 'success')
        return redirect(url_for('admin.manage_admissions'))

    except Exception as e:
        db.session.rollback()
        logging.exception("Approval process failed")
        flash(f"System error: {e}", "danger")

    return redirect(url_for('admin.manage_admissions'))


def assign_fees_to_student_tertiary(user, student_profile):
    """
    Assign fees to a newly admitted tertiary student.
    Matches by programme_name + programme_level (100).
    """
    # Find fee structure for this programme at Level 100
    fee_structures = ProgrammeFeeStructure.query.filter_by(
        programme_name=student_profile.current_programme,
        programme_level=str(student_profile.programme_level),  # ✅ use programme_level
        study_format=student_profile.study_format
    ).all()

    if not fee_structures:
        return False

    # Create StudentFeeBalance for each fee structure
    for fee_struct in fee_structures:
        balance = StudentFeeBalance(
            student_id=user.user_id,
            fee_structure_id=fee_struct.id,
            programme_name=student_profile.current_programme,
            programme_level=str(student_profile.programme_level),  # ✅ correct
            academic_year=student_profile.academic_year,
            semester=student_profile.semester,
            amount_due=fee_struct.amount,
            amount_paid=0.0,
            is_paid=False
        )

        db.session.add(balance)

    db.session.commit()
    return len(fee_structures) > 0


def generate_admission_letter(application, student_user):
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.pagesizes import A4
    import os

    folder = os.path.join("static/admission_letters")
    os.makedirs(folder, exist_ok=True)

    filename = f"admission_{student_user.user_id}.pdf"
    filepath = os.path.join(folder, filename)

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(filepath, pagesize=A4)

    story = []
    story.append(Paragraph("OFFICIAL ADMISSION LETTER", styles['Title']))
    story.append(Spacer(1, 20))

    story.append(Paragraph(f"Student Name: {application.other_names} {application.surname}", styles['Normal']))
    story.append(Paragraph(f"Student ID: {student_user.user_id}", styles['Normal']))
    story.append(Paragraph(f"Programme Offered: {application.admitted_programme}", styles['Normal']))
    story.append(Paragraph(f"Academic Year: {application.admitted_academic_year}", styles['Normal']))
    story.append(Spacer(1, 20))

    story.append(Paragraph(
        "Congratulations! You have been offered admission to this institution. "
        "Proceed to accept the offer and pay required school fees.",
        styles['Normal']
    ))

    doc.build(story)

    application.admission_letter_generated = True
    db.session.commit()


@admin_bp.route('/vouchers', methods=['GET', 'POST'])
def manage_vouchers():
    if request.method == 'POST':
        try:
            count = int(request.form.get('count', 1))
        except ValueError:
            count = 1

        amount_raw = request.form.get('amount', None)
        if amount_raw:
            try:
                amount = float(amount_raw)
            except ValueError:
                amount = float(current_app.config.get('VOUCHER_DEFAULT_AMOUNT', 50.0))
        else:
            amount = float(current_app.config.get('VOUCHER_DEFAULT_AMOUNT', 50.0))

        vouchers = []
        for _ in range(max(1, count)):
            pin = f"{random.randint(100000, 999999)}"
            serial = f"{random.randint(10000000, 99999999)}"
            v = AdmissionVoucher(pin=pin, serial=serial, amount=amount)
            db.session.add(v)
            vouchers.append(v)

        db.session.commit()
        flash(f'Generated {len(vouchers)} voucher(s).', 'success')
        return redirect(url_for('admin.manage_vouchers'))

    # Fetch all vouchers with related applicant info
    vouchers = AdmissionVoucher.query.order_by(AdmissionVoucher.created_at.desc()).all()
    return render_template('admin/vouchers.html', vouchers=vouchers)


@admin_bp.route('/vouchers/create', methods=['GET', 'POST'])
def create_voucher():
    if request.method == 'POST':
        # handle form submission here
        pass
    return render_template('admin/create_voucher.html')


@admin_bp.route('/logout')
@login_required
def logout():
    """Admin logout route"""
    from flask_login import logout_user
    logout_user()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('select_portal'))

