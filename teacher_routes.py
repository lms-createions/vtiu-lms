import csv
import json
import re
import tempfile
from zipfile import ZipFile
from flask import Blueprint, render_template, abort, flash, redirect, url_for, request, jsonify, current_app
from flask_login import login_required, current_user, login_user
import requests
from wtforms import SelectField
from models import CourseAssessmentScheme, CourseMaterial, ExamOption, ExamQuestion, ExamSet, ExamSetQuestion, Meeting, Option, Question, SemesterResultRelease, db, TeacherProfile, Course, StudentCourseRegistration, TeacherCourseAssignment, AttendanceRecord, User, StudentProfile, AcademicCalendar, AcademicYear, AppointmentBooking, AppointmentSlot, Assignment, Quiz, StudentQuizSubmission, Exam, ExamSubmission, AssignmentSubmission, GradingScale, ExamTimetableEntry, TeacherAssessment, TeacherAssessmentAnswer, TeacherAssessmentPeriod
from forms import AssignmentForm, ChangePasswordForm, ExamForm, ExamQuestionForm, ExamSetForm, MaterialForm, MeetingForm, QuizForm, TeacherLoginForm
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, date
from sqlalchemy import and_, desc, func, asc
from sqlalchemy.orm import joinedload
from collections import defaultdict
from utils.notifications import create_assignment_notification
from utils.notification_engine import notify_quiz_created, notify_assignment_created, notify_assignment_graded
import os, uuid
from utils.helpers import get_programme_choices, get_level_choices, get_course_choices
from utils.academic_year import configured_academic_year
from wtforms.validators import DataRequired 
from services.semester_grading_service import SemesterGradingService
import logging


teacher_bp = Blueprint("teacher", __name__, url_prefix="/teacher")

logger = logging.getLogger(__name__)


UPLOAD_FOLDER = 'static/uploads/quizzes'
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@teacher_bp.route('/login', methods=['GET', 'POST'])
def teacher_login():
    form = TeacherLoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        user_id = form.user_id.data.strip()
        password = form.password.data.strip()

        user = User.query.filter_by(user_id=user_id, role='teacher').first()
        if user and user.username.lower() == username.lower() and user.check_password(password):
            login_user(user)
            flash(f"Welcome back, {user.first_name}!", "success")
            return redirect(url_for('teacher.dashboard'))  # adjust dashboard endpoint
        flash("Invalid teacher credentials.", "danger")

    return render_template('teacher/login.html', form=form)

@teacher_bp.route('/dashboard')
@login_required
def dashboard():
    if current_user.role != 'teacher':
        abort(403)
    return render_template('teacher/dashboard.html', user=current_user)

@teacher_bp.route('/classes', methods=['GET', 'POST'])
@login_required
def classes():
    if current_user.role != 'teacher':
        abort(403)

    # 1) Ensure teacher profile exists
    profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    if not profile:
        flash("Please complete your profile before registering courses.", "warning")
        return redirect(url_for('teacher.dashboard'))

    # 2) Get all courses (ORDER BY PROGRAMME LEVEL NOW)
    all_courses = Course.query.order_by(Course.programme_level, Course.name).all()

    # 3) Courses already assigned to teacher
    assigned = {a.course_id for a in profile.assignments}

    # 4) Handle form submission
    if request.method == 'POST':
        selected = set(map(int, request.form.getlist('courses')))

        # Remove unchecked courses
        for a in profile.assignments[:]:
            if a.course_id not in selected:
                db.session.delete(a)

        # Add new ones
        for cid in selected - assigned:
            db.session.add(TeacherCourseAssignment(
                teacher_id=profile.id,
                course_id=cid
            ))

        db.session.commit()
        flash("Your course selections have been updated.", "success")
        return redirect(url_for('teacher.classes'))

    # 5) Build display list (PROGRAMME LEVEL)
    display = []
    for c in all_courses:
        display.append({
            'id': c.id,
            'programme_name': c.programme_name, 
            'programme_level': c.programme_level,
            'name': c.name,
            'registered': (c.id in assigned)
        })

    return render_template('teacher/classes.html', courses=display)

from flask import jsonify

@teacher_bp.route('/courses_for_programme', methods=['GET'])
@login_required
def courses_for_programme():
    """
    Get courses for a specific programme and level.
    Query parameters: programme (name), level (e.g., "100", "200", etc.)
    """
    if current_user.role != 'teacher':
        abort(403)

    programme = request.args.get('programme', '').strip()
    level = request.args.get('level', '').strip()

    if not programme or not level:
        return jsonify([]), 400

    # Query courses by programme_name and programme_level
    courses = Course.query.filter_by(
        programme_name=programme,
        programme_level=level
    ).order_by(Course.name).all()  # ✅ use 'name' here

    data = [
        {
            'id': c.id,
            'name': c.name,  # ✅ use 'name' here
            'code': c.code
        }
        for c in courses
    ]

    return jsonify(data)
    
@teacher_bp.route('/assessment_schemes')
@login_required
def assessment_scheme_list():
    if current_user.role != 'teacher':
        abort(403)

    profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    if not profile:
        flash("Please complete your profile first.", "warning")
        return redirect(url_for('teacher.dashboard'))

    # Only courses the teacher is registered to
    courses = [a.course for a in profile.assignments]

    return render_template('teacher/assessment_scheme_list.html', courses=courses)

@teacher_bp.route('/assessment_scheme/<int:course_id>', methods=['GET', 'POST'])
@login_required
def assessment_scheme(course_id):
    if current_user.role != 'teacher':
        abort(403)

    profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    course = Course.query.get_or_404(course_id)

    # Ensure teacher is assigned to this course
    if course not in [a.course for a in profile.assignments]:
        flash("You are not registered for this course.", "danger")
        return redirect(url_for('teacher.assessment_scheme_list'))

    # Determine current academic year from SchoolSettings
    from models import SchoolSettings
    settings = SchoolSettings.query.first()
    academic_year = configured_academic_year()

    # Fetch or create scheme
    scheme = CourseAssessmentScheme.query.filter_by(course_id=course_id, teacher_id=profile.id).first()

    if request.method == 'POST':
        quiz = float(request.form.get('quiz_weight', 0))
        assignment = float(request.form.get('assignment_weight', 0))
        exam = float(request.form.get('exam_weight', 0))

        total = quiz + assignment + exam
        if total != 100:
            flash(f"The total weight must equal 100%. Currently: {total}%", "danger")
            return render_template('teacher/assessment_scheme.html', course=course, scheme=scheme)

        if not scheme:
            scheme = CourseAssessmentScheme(
                course_id=course_id,
                teacher_id=profile.id,
                programme_name=course.programme_name,
                programme_level=course.programme_level,
                course_code=course.code,
                course_name=course.name,
                academic_year=academic_year,
                semester=course.semester
            )

        scheme.quiz_weight = quiz
        scheme.assignment_weight = assignment
        scheme.exam_weight = exam

        db.session.add(scheme)
        db.session.commit()
        flash("Assessment scheme saved successfully.", "success")
        return redirect(url_for('teacher.assessment_scheme_list'))

    return render_template('teacher/assessment_scheme.html', course=course, scheme=scheme)

@teacher_bp.route('/class/<int:course_id>')
@login_required
def view_class(course_id):
    if current_user.role != 'teacher':
        abort(403)

    course = Course.query.get_or_404(course_id)
    registrations = StudentCourseRegistration.query.filter_by(course_id=course_id).all()

    return render_template('teacher/class_detail.html', course=course, registrations=registrations)

@teacher_bp.route('/materials')
@login_required
def manage_materials():
    materials = CourseMaterial.query.order_by(CourseMaterial.upload_date.desc()).all()
    return render_template('teacher/manage_materials.html', materials=materials)

@teacher_bp.route('/get_courses/<programme>/<level>', methods=['GET'])
@login_required
def get_courses(programme, level):
    """
    Get courses for a specific programme and level.
    Returns JSON list of courses.
    URL: /teacher/get_courses/{programme}/{level}
    """
    if current_user.role not in ['teacher', 'teacher']:
        abort(403)

    # Decode URL-encoded parameters
    from urllib.parse import unquote
    programme = unquote(programme)
    level = unquote(level)

    print(f"Getting courses for programme='{programme}', level='{level}'")

    try:
        # Query courses by programme_name and programme_level
        courses = Course.query.filter_by(
            programme_name=programme,
            programme_level=level
        ).order_by(Course.name).all()

        print(f"Found {len(courses)} courses")

        # Return JSON with correct field names
        # ✅ FIX: Use c.name NOT c.course_name
        data = [
            {
                'id': c.id,
                'name': c.name,      # ✅ CORRECT - Course model uses 'name'
                'code': c.code
            }
            for c in courses
        ]

        return jsonify(data)
    
    except Exception as e:
        print(f"Error in get_courses: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    
@teacher_bp.route('/materials/add', methods=['GET', 'POST'])
@login_required
def add_material():
    """Allow teachers to upload course materials."""
    if current_user.role != 'teacher':
        abort(403)

    form = MaterialForm()

    # Populate programme choices
    form.programme_name.choices = [
        ('', '— Select Programme —'),
        ('Cyber Security', 'Cyber Security'),
        ('Early Childhood Education', 'Early Childhood Education'),
        ('Dispensing Technician II & III', 'Dispensing Technician II & III'),
        ('Diagnostic Medical Sonography', 'Diagnostic Medical Sonography'),
        ('Medical Laboratory Technology', 'Medical Laboratory Technology'),
        ('Dispensing Assistant', 'Dispensing Assistant'),
        ('Health Information Management', 'Health Information Management'),
        ('Optical Technician', 'Optical Technician'),
        ('Midwifery', 'Midwifery'),
        ('Ophthalmic Dispensing', 'Ophthalmic Dispensing'),
        ('HND Dispensing Technology', 'HND Dispensing Technology'),
        ('Diploma in Early Childhood Education', 'Diploma in Early Childhood Education')
    ]

    # Populate level choices
    form.programme_level.choices = [
        ('', '— Select Level —'),
        ('100', 'Level 100'),
        ('200', 'Level 200'),
        ('300', 'Level 300'),
        ('400', 'Level 400')
    ]

    # Get selected values from form data (POST) or form object
    selected_programme = request.form.get('programme_name', '') or form.programme_name.data or ''
    selected_level = request.form.get('programme_level', '') or form.programme_level.data or ''

    # Load courses based on programme + level
    course_list = []
    if selected_programme and selected_level:
        course_list = Course.query.filter_by(
            programme_name=selected_programme,
            programme_level=selected_level
        ).order_by(Course.name).all()

    # Update course choices
    form.course_name.choices = [('', '— Select Course (Optional) —')] + [
        (c.name, f"{c.code} — {c.name}") for c in course_list
    ]

    # Handle GET request - just show the form
    if request.method == 'GET':
        return render_template('teacher/add_materials.html', form=form)

    # ============ HANDLE POST REQUEST ============
    print("\n" + "="*60)
    print("MATERIALS FORM SUBMISSION DEBUG")
    print("="*60)
    print(f"Form validates: {form.validate()}")
    if form.errors:
        print(f"Form Errors: {form.errors}")
        for field, errors in form.errors.items():
            flash(f"{field}: {', '.join(errors)}", "danger")
        return render_template('teacher/add_materials.html', form=form)

    print("Form is valid")
    print("="*60 + "\n")

    try:
        # Extract form data
        programme_name = form.programme_name.data.strip()
        programme_level = form.programme_level.data.strip()
        title = form.title.data.strip()
        course_name = form.course_name.data

        # Get course if selected
        course = None
        if course_name and str(course_name).strip():
            course = Course.query.filter_by(
                name=course_name,
                programme_name=programme_name,
                programme_level=programme_level
            ).first()
            
            if not course:
                flash("Selected course not found.", "danger")
                return render_template('teacher/add_materials.html', form=form)

        saved_count = 0
        
        for file in form.files.data:
            if not file or not file.filename:
                continue

            filename = secure_filename(file.filename)
            file_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

            # Handle ZIP files
            if file_ext == 'zip':
                with tempfile.TemporaryDirectory() as tmpdir:
                    zip_path = os.path.join(tmpdir, filename)
                    file.save(zip_path)

                    try:
                        with ZipFile(zip_path) as zip_ref:
                            zip_ref.extractall(tmpdir)

                        for root, _, files in os.walk(tmpdir):
                            for inner_file in files:
                                if inner_file.endswith((
                                    '.jpg', '.jpeg', '.png', '.mp3', '.mp4', '.mov', '.avi',
                                    '.doc', '.docx', '.xls', '.xlsx', '.pdf', '.ppt', '.pptx', '.txt'
                                )):
                                    in_path = os.path.join(root, inner_file)
                                    with open(in_path, 'rb') as f:
                                        data = f.read()

                                    orig_name = secure_filename(inner_file)
                                    unique_name = f"{uuid.uuid4().hex}_{orig_name}"
                                    save_path = os.path.join(current_app.config['MATERIALS_FOLDER'], unique_name)
                                    
                                    os.makedirs(current_app.config['MATERIALS_FOLDER'], exist_ok=True)
                                    with open(save_path, 'wb') as out:
                                        out.write(data)

                                    db.session.add(CourseMaterial(
                                        title=title,
                                        course_name=course.name if course else None,
                                        programme_name=programme_name,
                                        programme_level=programme_level,
                                        filename=unique_name,
                                        original_name=orig_name,
                                        file_type=orig_name.rsplit('.', 1)[-1].lower()
                                    ))
                                    saved_count += 1
                    except ZipFile.BadZipFile:
                        flash(f"Invalid ZIP file: {filename}", "warning")
                        continue
            else:
                # Handle non-zip files
                if filename.endswith(('.jpg', '.jpeg', '.png', '.mp3', '.mp4', '.mov', '.avi',
                                     '.doc', '.docx', '.xls', '.xlsx', '.pdf', '.ppt', '.pptx', '.txt')):
                    orig_name = filename
                    unique_name = f"{uuid.uuid4().hex}_{orig_name}"
                    save_path = os.path.join(current_app.config['MATERIALS_FOLDER'], unique_name)
                    
                    os.makedirs(current_app.config['MATERIALS_FOLDER'], exist_ok=True)
                    file.save(save_path)

                    db.session.add(CourseMaterial(
                        title=title,
                        course_name=course.name if course else None,
                        programme_name=programme_name,
                        programme_level=programme_level,
                        filename=unique_name,
                        original_name=orig_name,
                        file_type=orig_name.rsplit('.', 1)[-1].lower()
                    ))
                    saved_count += 1

        if saved_count > 0:
            db.session.commit()
            print(f"{saved_count} material(s) uploaded successfully")
            flash(f"✓ {saved_count} material(s) uploaded successfully!", "success")
            return redirect(url_for("teacher.manage_materials"))
        else:
            flash("No valid files were uploaded.", "warning")
            return redirect(request.url)

    except Exception as e:
        db.session.rollback()
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f"Error uploading materials: {str(e)}", "danger")
        return render_template('teacher/add_materials.html', form=form)
    
@teacher_bp.route('/materials/edit/<int:material_id>', methods=['GET', 'POST'])
def edit_material(material_id):
    material = CourseMaterial.query.get_or_404(material_id)
    form = MaterialForm(obj=material)

    if form.validate_on_submit():
        form.populate_obj(material)
        db.session.commit()
        flash("Material updated successfully!", "success")
        return redirect(url_for('teacher.manage_materials'))

    return render_template('teacher/edit_material.html', form=form, material=material)

@teacher_bp.route('/materials/delete/<int:material_id>', methods=['POST'])
@login_required
def delete_material(material_id):
    material = CourseMaterial.query.get_or_404(material_id)
    path = os.path.join(current_app.config['MATERIALS_FOLDER'], material.filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(material)
    db.session.commit()
    flash('Material deleted.', 'info')
    return redirect(url_for('teacher.manage_materials'))

@teacher_bp.route('/manage-assignments')
@login_required
def manage_assignments():
    assignments = Assignment.query.order_by(Assignment.due_date.asc()).all()
    return render_template('teacher/manage_assignments.html', assignments=assignments)

def get_courses_for_programme(programme_name):
    """
    Returns a list of tuples (course_id, course_name) for a given programme.
    """
    courses = Course.query.filter_by(programme_name=programme_name).all()
    return [(course.id, course.name) for course in courses]

@teacher_bp.route('/assignments/add', methods=['GET', 'POST'])
@login_required
def add_assignment():
    if current_user.role != "teacher":
        abort(403)

    form = AssignmentForm()
    
    # Populate programme choices
    form.programme.choices = get_programme_choices()

    if request.method == 'GET':
        # Just render the form
        return render_template('teacher/add_assignment.html', form=form)

    # ============ POST REQUEST ============
    # Get values from request.form (not from form object validation)
    programme_selected = request.form.get('programme', '').strip()
    level_selected = request.form.get('programme_level', '').strip()
    course_id = request.form.get('course_id', '').strip()

    # Debug logging
    print(f"Form submission - Programme: {programme_selected}, Level: {level_selected}, Course ID: {course_id}")
    print(f"Form errors: {form.errors}")

    # Validate required fields
    if not programme_selected:
        flash("Please select a programme.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    if not level_selected:
        flash("Please select a programme level.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    if not course_id:
        flash("Please select a course.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    # Convert course_id to integer
    try:
        course_id = int(course_id)
    except (ValueError, TypeError):
        flash("Invalid course selected.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    # Verify course exists
    course = Course.query.get(course_id)
    if not course:
        flash("Selected course does not exist.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    # Validate form fields (title, description, etc.)
    if not form.title.data or not form.title.data.strip():
        flash("Assignment title is required.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    if not form.due_date.data:
        flash("Due date is required.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    if not form.max_score.data or form.max_score.data <= 0:
        flash("Max score must be greater than 0.", "danger")
        return render_template('teacher/add_assignment.html', form=form)

    # Handle file upload
    file = form.file.data
    filename, original_name = None, None
    if file and file.filename:
        try:
            original_name = file.filename
            filename = secure_filename(original_name)
            if filename:
                os.makedirs(current_app.config['UPLOAD_FOLDER'], exist_ok=True)
                file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
        except Exception as e:
            print(f"File upload error: {e}")
            flash(f"Error uploading file: {e}", "danger")
            return render_template('teacher/add_assignment.html', form=form)

    # Create assignment
    try:
        assignment = Assignment(
            title=form.title.data.strip(),
            description=form.description.data.strip() if form.description.data else '',
            instructions=form.instructions.data.strip() if form.instructions.data else '',
            course_id=course.id,
            course_name=course.name,
            programme_name=programme_selected,
            programme_level=level_selected,
            due_date=form.due_date.data,
            max_score=form.max_score.data,
            filename=filename,
            original_name=original_name
        )

        db.session.add(assignment)
        db.session.commit()
        
        # Notify students about assignment
        try:
            notify_assignment_created(assignment, send_email=True)
        except Exception as e:
            current_app.logger.warning(f"Assignment notification failed: {e}")

        flash("✓ Assignment added successfully!", "success")
        return redirect(url_for('teacher.manage_assignments'))

    except Exception as e:
        db.session.rollback()
        print(f"Error creating assignment: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error saving assignment: {e}", "danger")
        return render_template('teacher/add_assignment.html', form=form)
    
@teacher_bp.route('/assignments/edit/<int:assignment_id>', methods=['GET', 'POST'])
@login_required
def edit_assignment(assignment_id):
    if current_user.role != "teacher":
        abort(403)

    assignment = Assignment.query.get_or_404(assignment_id)
    form = AssignmentForm(obj=assignment)

    # populate programme choices and levels
    form.programme.choices = get_programme_choices()  # [(name, name), ...]
    form.programme_level.choices = [('100','100'),('200','200'),('300','300'),('400','400')]

    # Fetch teacher's courses (only those assigned to teacher) - use id and name
    teacher_courses = (
        Course.query
        .join(TeacherCourseAssignment)
        .filter(TeacherCourseAssignment.teacher_id == current_user.id)
        .all()
    )
    form.course_name.choices = [(str(c.id), c.name) for c in teacher_courses]

    # pre-select form values on GET
    if request.method == "GET":
        form.programme.data = assignment.programme_name or ''
        form.programme_level.data = assignment.programme_level or ''
        form.course_name.data = str(assignment.course_id)

    if form.validate_on_submit():
        # get course object
        course_id = int(form.course_name.data)
        course = Course.query.get(course_id)
        if not course:
            flash("Invalid course selected.", "danger")
            return redirect(request.url)

        assignment.title = form.title.data.strip()
        assignment.description = form.description.data or ''
        assignment.instructions = form.instructions.data or ''
        assignment.programme_name = form.programme.data
        assignment.programme_level = form.programme_level.data
        assignment.due_date = form.due_date.data
        assignment.max_score = form.max_score.data
        assignment.course_id = course.id
        assignment.course_name = course.name

        file = form.file.data
        if file and file.filename:
            original_name = file.filename
            filename = secure_filename(original_name)
            os.makedirs(current_app.config['UPLOAD_FOLDER'], exist_ok=True)
            file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
            assignment.filename = filename
            assignment.original_name = original_name

        db.session.commit()
        flash('Assignment updated successfully.', 'success')
        return redirect(url_for('teacher.manage_assignments'))

    return render_template('teacher/edit_assignment.html', form=form, assignment=assignment)

@teacher_bp.route('/assignments/delete/<int:assignment_id>', methods=['POST'])
@login_required
def delete_assignment(assignment_id):
    assignment = Assignment.query.get_or_404(assignment_id)

    # Delete uploaded file if exists
    if assignment.filename:
        file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], assignment.filename)
        if os.path.exists(file_path):
            os.remove(file_path)

    db.session.delete(assignment)
    db.session.commit()
    flash('Assignment deleted successfully.', 'success')
    return redirect(url_for('teacher.manage_assignments'))

@teacher_bp.route('/submissions')
@login_required
def submissions_index():
    if current_user.role != 'teacher':
        abort(403)

    assignments = Assignment.query.order_by(Assignment.course_name, Assignment.due_date.desc()).all()
    grouped = defaultdict(list)
    for a in assignments:
        grouped[a.course_name or 'General'].append(a)
    grouped = dict(grouped)

    # force the teacher base template explicitly
    return render_template('teacher/submissions_index.html', grouped=grouped, layout='teacher/base_teacher.html')

@teacher_bp.route('/assignment/<int:assignment_id>/submissions')
@login_required
def view_submissions(assignment_id):
    assignment = Assignment.query.get_or_404(assignment_id)
    submissions = AssignmentSubmission.query.filter_by(assignment_id=assignment_id).all()
    return render_template(
        "teacher/assignment_submissions.html",
        assignment=assignment,
        submissions=submissions
    )

@teacher_bp.route('/submission/<int:submission_id>/score', methods=['GET', 'POST'])
@login_required
def score_submission(submission_id):
    submission = AssignmentSubmission.query.get_or_404(submission_id)

    if request.method == 'POST':
        score = request.form.get("score")
        feedback = request.form.get("feedback")

        submission.score = float(score) if score else None
        submission.feedback = feedback
        submission.scored_at = datetime.utcnow()

        # Automatically assign grade if grading scale exists
        scales = GradingScale.query.order_by(GradingScale.min_score.desc()).all()
        for scale in scales:
            if submission.score is not None and scale.min_score <= submission.score <= scale.max_score:
                submission.grade_letter = scale.grade_letter
                submission.pass_fail = scale.pass_fail
                break

        db.session.commit()
        
        # Notify student about grade
        try:
            student = User.query.filter_by(user_id=submission.student_id).first()
            if student:
                notify_assignment_graded(
                    submission.assignment_id, 
                    submission.student_id, 
                    submission.score or 0,
                    submission.feedback or '',
                    send_email=True
                )
        except Exception as e:
            current_app.logger.warning(f"Grade notification failed: {e}")
        
        flash("Score saved successfully.", "success")
        return redirect(url_for('teacher.view_submissions', assignment_id=submission.assignment_id))

    return render_template("teacher/score_submission.html", submission=submission)

@teacher_bp.route('/quizzes')
def manage_quizzes():
    quizzes = Quiz.query.order_by(Quiz.start_datetime.desc()).all()
    now = datetime.utcnow()

    upcoming = [q for q in quizzes if q.start_datetime > now]
    ongoing = [q for q in quizzes if q.start_datetime <= now <= q.end_datetime]
    past = [q for q in quizzes if q.end_datetime < now]

    return render_template(
        'teacher/manage_quizzes.html',
        quizzes=quizzes,
        now=now,
        upcoming_count=len(upcoming),
        ongoing_count=len(ongoing),
        past_count=len(past),
        current_app=current_app  # <-- pass it here
    )

@teacher_bp.route('/edit/<model>/<int:record_id>', methods=['GET', 'POST'])
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
        return redirect(url_for('teacher.list_records', model=model))

    # Render edit template
    return render_template('teacher/edit_record.html', record=record, classes=classes, model=model)


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

def get_course_choices(programme_name):
    courses = Course.query.filter_by(programme_name=programme_name).all()
    return [(c.name, c.name) for c in courses]

@teacher_bp.route('/add_quiz', methods=['GET', 'POST'])
@login_required
def add_quiz():
    """Create a new quiz."""
    if current_user.role != 'teacher':
        abort(403)

    form = QuizForm()

    # Populate programme choices
    form.assigned_programme.choices = [
        ('', '— Select Programme —'),
        ('Cyber Security', 'Cyber Security'),
        ('Early Childhood Education', 'Early Childhood Education'),
        ('Dispensing Technician II & III', 'Dispensing Technician II & III'),
        ('Diagnostic Medical Sonography', 'Diagnostic Medical Sonography'),
        ('Medical Laboratory Technology', 'Medical Laboratory Technology'),
        ('Dispensing Assistant', 'Dispensing Assistant'),
        ('Health Information Management', 'Health Information Management'),
        ('Optical Technician', 'Optical Technician'),
        ('Midwifery', 'Midwifery'),
        ('Ophthalmic Dispensing', 'Ophthalmic Dispensing'),
        ('HND Dispensing Technology', 'HND Dispensing Technology'),
        ('Diploma in Early Childhood Education', 'Diploma in Early Childhood Education')
    ]

    # Populate level choices
    form.programme_level.choices = [
        ('', '— Select Level —'),
        ('100', 'Level 100'),
        ('200', 'Level 200'),
        ('300', 'Level 300'),
        ('400', 'Level 400')
    ]

    # Get selected values from form data (POST) or form object
    selected_programme = request.form.get('assigned_programme', '') or form.assigned_programme.data or ''
    selected_level = request.form.get('programme_level', '') or form.programme_level.data or ''

    # Load courses based on programme + level
    course_list = []
    course_list = Course.query.filter_by(
        programme_name=selected_programme,
        programme_level=selected_level
    ).order_by(Course.name).all()

    # Update course choices
    form.course_name.choices = [('', '— Select Course (Optional) —')] + [
        (c.name, f"{c.code} — {c.name}") for c in course_list
    ]
    form.course_id.choices = [('', '')] + [
        (str(c.id), str(c.id)) for c in course_list
    ]

    # Handle GET request - just show the form
    if request.method == 'GET':
        return render_template('teacher/add_quiz.html', form=form)

    # ============ HANDLE POST REQUEST ============
    print("\n" + "="*60)
    print("FORM SUBMISSION DEBUG")
    print("="*60)
    print(f"Form validates: {form.validate()}")
    if form.errors:
        print(f"Form Errors: {form.errors}")
        for field, errors in form.errors.items():
            flash(f"{field}: {', '.join(errors)}", "danger")
        return render_template('teacher/add_quiz.html', form=form)

    print("Form is valid")
    print("="*60 + "\n")

    try:
        # Extract form data
        programme_name = form.assigned_programme.data.strip()
        programme_level = form.programme_level.data.strip()
        title = form.title.data.strip()
        start_datetime = form.start_datetime.data
        end_datetime = form.end_datetime.data
        duration = int(form.duration.data)
        course_name = form.course_name.data

        # Validate dates
        if start_datetime >= end_datetime:
            flash("Start date/time must be before end date/time.", "danger")
            return render_template('teacher/add_quiz.html', form=form)

        # Get course if selected
        course = None
        if course_name and str(course_name).strip():
            course = Course.query.filter_by(
                name=course_name,
                programme_name=programme_name,
                programme_level=programme_level
            ).first()
            
            if not course:
                flash("Selected course not found.", "danger")
                return render_template('teacher/add_quiz.html', form=form)

        # Check for duplicate quiz
        dup_query = Quiz.query.filter_by(
            title=title,
            programme_name=programme_name,
            programme_level=programme_level
        )
        if course:
            dup_query = dup_query.filter_by(course_id=course.id)
        else:
            dup_query = dup_query.filter(Quiz.course_id.is_(None))

        if dup_query.first():
            flash("A quiz with this title already exists for this programme/level/course.", "danger")
            return render_template('teacher/add_quiz.html', form=form)

        # Check for time overlap
        overlap = Quiz.query.filter(
            Quiz.programme_name == programme_name,
            Quiz.programme_level == programme_level,
            Quiz.start_datetime < end_datetime,
            Quiz.end_datetime > start_datetime
        )
        if course:
            overlap = overlap.filter_by(course_id=course.id)
        else:
            overlap = overlap.filter(Quiz.course_id.is_(None))

        if overlap.first():
            flash("Another quiz is scheduled during this time.", "danger")
            return render_template('teacher/add_quiz.html', form=form)

        # Create quiz
        quiz = Quiz(
            programme_name=programme_name,
            programme_level=programme_level,
            course_id=course.id if course else None,
            course_name=course.name if course else None,
            title=title,
            date=start_datetime.date(),
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            duration_minutes=duration,
        )

        db.session.add(quiz)
        db.session.flush()

        # Save questions
        questions_added = 0
        for key in request.form.keys():
            match = re.match(r'^questions\[(\d+)\]\[text\]$', key)
            if not match:
                continue

            q_index = match.group(1)
            question_text = request.form.get(key, '').strip()

            if not question_text:
                continue

            # Detect question type
            blanks = re.findall(r'_{3,}', question_text)
            q_type = 'fill_in' if blanks else request.form.get(f'questions[{q_index}][type]', 'mcq')

            # Create question
            question = Question(
                quiz_id=quiz.id,
                text=question_text,
                question_type=q_type
            )
            db.session.add(question)
            db.session.flush()
            questions_added += 1

            # Add MCQ options
            if q_type == 'mcq':
                o_idx = 0
                while f'questions[{q_index}][options][{o_idx}][text]' in request.form:
                    opt_text = request.form.get(f'questions[{q_index}][options][{o_idx}][text]', '').strip()
                    is_correct = f'questions[{q_index}][options][{o_idx}][is_correct]' in request.form

                    if opt_text:
                        db.session.add(Option(
                            question_id=question.id,
                            text=opt_text,
                            is_correct=is_correct
                        ))
                    o_idx += 1

            # Add fill-in answers
            elif q_type == 'fill_in':
                a_idx = 0
                while f'questions[{q_index}][answers][{a_idx}]' in request.form:
                    ans_text = request.form.get(f'questions[{q_index}][answers][{a_idx}]', '').strip()

                    if ans_text:
                        db.session.add(Option(
                            question_id=question.id,
                            text=ans_text,
                            is_correct=True
                        ))
                    a_idx += 1

        if questions_added == 0:
            db.session.rollback()
            flash("Quiz must have at least one question.", "danger")
            return render_template('teacher/add_quiz.html', form=form)

        # Commit all changes
        db.session.commit()

        # Notify students about the new quiz
        try:
            notify_quiz_created(quiz, send_email=True)
        except Exception as e:
            current_app.logger.warning(f"Failed to send quiz notification: {e}")

        print(f"Quiz '{title}' created with {questions_added} question(s)")
        flash(f"Quiz '{title}' created successfully with {questions_added} question(s). Students have been notified.", "success")
        return redirect(url_for('teacher.manage_quizzes'))

    except Exception as e:
        db.session.rollback()
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f"Error: {str(e)}", "danger")
        return render_template('teacher/add_quiz.html', form=form)
    
def is_quiz_active(quiz):
    now = datetime.now()
    quiz_start = datetime.combine(quiz.date, quiz.start_time)
    quiz_end = quiz_start + timedelta(minutes=quiz.duration_minutes)
    return quiz_start <= now <= quiz_end

@teacher_bp.route('/edit_quiz/<int:quiz_id>', methods=['GET', 'POST'])
@login_required
def edit_quiz(quiz_id):
    """Edit an existing quiz - tertiary education (programme/level based)"""
    quiz = Quiz.query.options(
        joinedload(Quiz.questions).joinedload(Question.options)
    ).get_or_404(quiz_id)
    form = QuizForm()

    # Populate programme choices (same as add_quiz)
    form.assigned_programme.choices = [
        ('', '— Select Programme —'),
        ('Cyber Security', 'Cyber Security'),
        ('Early Childhood Education', 'Early Childhood Education'),
        ('Dispensing Technician II & III', 'Dispensing Technician II & III'),
        ('Diagnostic Medical Sonography', 'Diagnostic Medical Sonography'),
        ('Medical Laboratory Technology', 'Medical Laboratory Technology'),
        ('Dispensing Assistant', 'Dispensing Assistant'),
        ('Health Information Management', 'Health Information Management'),
        ('Optical Technician', 'Optical Technician'),
        ('Midwifery', 'Midwifery'),
        ('Ophthalmic Dispensing', 'Ophthalmic Dispensing'),
        ('HND Dispensing Technology', 'HND Dispensing Technology'),
        ('Diploma in Early Childhood Education', 'Diploma in Early Childhood Education')
    ]

    # Populate level choices
    form.programme_level.choices = [
        ('', '— Select Level —'),
        ('100', 'Level 100'),
        ('200', 'Level 200'),
        ('300', 'Level 300'),
        ('400', 'Level 400')
    ]

    # Determine selected programme/level (POST > form data > quiz defaults)
    selected_programme = request.form.get('assigned_programme', '') or form.assigned_programme.data or getattr(quiz, 'programme_name', '') or ''
    selected_level = request.form.get('programme_level', '') or form.programme_level.data or getattr(quiz, 'programme_level', '') or ''

    # Load courses based on programme + level and populate course choices
    course_list = Course.query.filter_by(
        programme_name=selected_programme,
        programme_level=selected_level
    ).order_by(Course.name).all()

    form.course_name.choices = [('', '— Select Course (Optional) —')] + [
        (c.name, f"{getattr(c, 'code', '')} — {c.name}") for c in course_list
    ]
    form.course_id.choices = [('', '')] + [
        (str(c.id), str(c.id)) for c in course_list
    ]

    # Helper to build quiz questions payload
    def build_quiz_questions_payload(qz):
        payload = []
        for q in qz.questions:
            payload.append({
                "id": q.id,
                "text": q.text,
                "type": q.question_type,
                "label_style": "abc",
                "options": [
                    {
                        "id": o.id,
                        "text": o.text,
                        "is_correct": bool(o.is_correct)
                    }
                    for o in q.options
                ]
            })
        return payload

    # Helper to render edit form with data persisted
    def render_edit_form(form_obj, quiz_obj, questions_payload):
        return render_template(
            'teacher/edit_quiz.html',
            form=form_obj,
            quiz=quiz_obj,
            selected_course_id=quiz_obj.course_id,
            quiz_questions=questions_payload
        )

    # GET - Populate form with existing quiz data
    if request.method == 'GET':
        form.assigned_programme.data = getattr(quiz, 'programme_name', '')
        form.programme_level.data = getattr(quiz, 'programme_level', '')
        form.title.data = quiz.title
        form.course_id.data = quiz.course_id
        form.course_name.data = quiz.course_name
        # ✅ IMPORTANT: Duration is a SelectField with STRING choices
        form.duration.data = str(quiz.duration_minutes)
        form.attempts_allowed.data = quiz.attempts_allowed
        form.start_datetime.data = quiz.start_datetime
        form.end_datetime.data = quiz.end_datetime
        
        return render_edit_form(form, quiz, build_quiz_questions_payload(quiz))

    # POST - Validate and save
    if not form.validate_on_submit():
        # ✅ On validation error, persist form data including questions
        # Re-populate form from POST data so user sees their input
        form.assigned_programme.data = request.form.get('assigned_programme', getattr(quiz, 'programme_name', ''))
        form.programme_level.data = request.form.get('programme_level', getattr(quiz, 'programme_level', ''))
        form.title.data = request.form.get('title', quiz.title)
        form.course_id.data = request.form.get('course_id', quiz.course_id, type=int)
        form.course_name.data = request.form.get('course_name', quiz.course_name)
        form.duration.data = request.form.get('duration', str(quiz.duration_minutes))
        form.attempts_allowed.data = request.form.get('attempts_allowed', quiz.attempts_allowed, type=int)
        
        # Flash validation errors
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{field}: {error}", "danger")
        
        return render_edit_form(form, quiz, build_quiz_questions_payload(quiz))

    try:
        # BASIC FIELDS
        title = form.title.data.strip()
        start_datetime = form.start_datetime.data
        end_datetime = form.end_datetime.data
        # ✅ Convert string duration back to int
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
                points=1.0
            )
            db.session.add(question)
            db.session.flush()

            # ✅ SET correct_option_id FOR MCQ
            if q_type == 'mcq':
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
                        
                        if is_correct:
                            correct_option_id = option.id
                    
                    o_index += 1
                
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
        return redirect(url_for('teacher.manage_quizzes'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Error editing quiz {quiz_id}: {e}")
        flash(f"Error updating quiz: {e}", "danger")
        return redirect(request.url)
            
@teacher_bp.route('/quizzes/delete/<int:quiz_id>', methods=['POST'])
@login_required
def delete_quiz(quiz_id):
    quiz = Quiz.query.get_or_404(quiz_id)
    db.session.delete(quiz)
    db.session.commit()
    flash("Quiz deleted successfully.", "success")
    return redirect(url_for('teacher.manage_quizzes'))

@teacher_bp.route('/restore_quiz', methods=['GET', 'POST'])
@login_required
def restore_quiz():
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
            return redirect(url_for('teacher.manage_quizzes'))

        except Exception as e:
            db.session.rollback()
            flash(f"Error restoring quiz: {e}", "danger")
            return redirect(request.url)

    return render_template("teacher/restore_quiz.html")

@teacher_bp.route('/attendance', methods=['GET', 'POST'])
@login_required
def attendance():
    if current_user.role != 'teacher':
        abort(403)
    teacher = TeacherProfile.query.filter_by(user_id=current_user.user_id).first_or_404()

    # 1️⃣ Get programme + level combinations the teacher teaches
    programmes = sorted({a.course.programme_name for a in teacher.assignments})
    levels = sorted({a.course.programme_level for a in teacher.assignments})

    # 2️⃣ Filters from UI
    selected_programme = request.values.get('programme', '')
    selected_level     = request.values.get('level', '')
    date_str           = request.values.get('date', '')

    today = datetime.utcnow().date()
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today
    except ValueError:
        selected_date = today

    # 3️⃣ Students in that programme & level
    students = []
    if selected_programme and selected_level:
        students = (User.query.join(StudentProfile, StudentProfile.user_id == User.user_id).filter(StudentProfile.current_programme == selected_programme, StudentProfile.programme_level == selected_level).order_by(User.last_name).all())

    # 4️⃣ Existing attendance
    existing_records = {
        r.student_id
        for r in AttendanceRecord.query.filter(AttendanceRecord.teacher_id == teacher.id, AttendanceRecord.date == selected_date)}

    # 5️⃣ Disabled dates from calendar
    cal_entries = AcademicCalendar.query.with_entities(
        AcademicCalendar.date, AcademicCalendar.break_type
    ).all()

    disabled_dates = {entry.date.isoformat(): entry.break_type for entry in cal_entries}

    # 6️⃣ Save attendance
    if request.method == 'POST' and request.form.get('action') == 'submit_attendance':
        inserted = duplicates = 0

        for student in students:
            if student.user_id in existing_records:
                duplicates += 1
                continue

            present = bool(request.form.get(f'attend_{student.user_id}'))
            db.session.add(AttendanceRecord(
                student_id=student.user_id,
                teacher_id=teacher.id,
                date=selected_date,
                is_present=present
            ))
            inserted += 1

        db.session.commit()

        if inserted:
            flash(f"{inserted} new record(s) saved.", "success")
        if duplicates:
            flash(f"{duplicates} student(s) already marked and skipped.", "warning")

        return redirect(url_for('teacher.attendance', programme=selected_programme, level=selected_level, date=selected_date.isoformat()))

    return render_template('teacher/attendance.html', programmes=programmes, levels=levels, students=students, existing_records=existing_records, selected_programme=selected_programme, selected_level=selected_level, selected_date=selected_date, disabled_dates=disabled_dates)

@teacher_bp.route('/view-attendance')
@login_required
def view_attendance():
    """View student attendance (tertiary version with levels & programmes)"""
    if current_user.role != 'teacher':
        abort(403)

    teacher = TeacherProfile.query.filter_by(user_id=current_user.user_id).first_or_404()
    
    # Filter by programme level instead of class
    selected_level = request.args.get('levelSelect', '', type=str)
    selected_programme = request.args.get('programmeSelect', '', type=str)
    selected_date_str = request.args.get('date', '', type=str)
    selected_date = None
    
    if selected_date_str:
        try:
            selected_date = datetime.fromisoformat(selected_date_str).date()
        except ValueError:
            selected_date = None

    # Get all distinct dates for the selected programme/level
    date_query = db.session.query(AttendanceRecord.date).filter(
        AttendanceRecord.teacher_id == teacher.id
    )
    if selected_level:
        date_query = date_query.join(User, User.id == AttendanceRecord.student_id) \
                               .join(StudentProfile, StudentProfile.user_id == User.user_id) \
                               .filter(StudentProfile.programme_level == int(selected_level))
    if selected_programme:
        date_query = date_query.join(User, User.id == AttendanceRecord.student_id) \
                               .join(StudentProfile, StudentProfile.user_id == User.user_id) \
                               .filter(StudentProfile.current_programme == selected_programme)
    
    date_list = [d[0] for d in date_query.distinct().order_by(AttendanceRecord.date).all()]

    # Get students for the selected programme/level
    student_query = db.session.query(
        User.id, User.first_name, User.middle_name, User.last_name,
        StudentProfile.current_programme, StudentProfile.programme_level
    ).join(StudentProfile, StudentProfile.user_id == User.user_id) \
     .join(AttendanceRecord, AttendanceRecord.student_id == User.id) \
     .filter(AttendanceRecord.teacher_id == teacher.id)
    
    if selected_level:
        student_query = student_query.filter(StudentProfile.programme_level == int(selected_level))
    if selected_programme:
        student_query = student_query.filter(StudentProfile.current_programme == selected_programme)
    
    students = student_query.distinct().order_by(User.last_name, User.first_name).all()

    # Get attendance records filtered by date and/or programme/level
    attendance_query = db.session.query(
        AttendanceRecord.student_id, AttendanceRecord.date, AttendanceRecord.is_present
    ).filter(AttendanceRecord.teacher_id == teacher.id)
    
    if selected_level:
        attendance_query = attendance_query.join(User, User.id == AttendanceRecord.student_id) \
                                           .join(StudentProfile, StudentProfile.user_id == User.user_id) \
                                           .filter(StudentProfile.programme_level == int(selected_level))
    if selected_programme:
        attendance_query = attendance_query.join(User, User.id == AttendanceRecord.student_id) \
                                           .join(StudentProfile, StudentProfile.user_id == User.user_id) \
                                           .filter(StudentProfile.current_programme == selected_programme)
    if selected_date:
        attendance_query = attendance_query.filter(AttendanceRecord.date == selected_date)
    
    attendance_records = attendance_query.all()

    # Attendance map for Excel-style table
    attendance_map = defaultdict(lambda: 0)
    for student_id, date, is_present in attendance_records:
        attendance_map[(student_id, date)] = 1 if is_present else 0

    # Records list for standard table
    records_query = db.session.query(
        AttendanceRecord.date, StudentProfile.current_programme, StudentProfile.programme_level,
        User.first_name, User.middle_name, User.last_name, AttendanceRecord.is_present
    ).join(User, User.id == AttendanceRecord.student_id) \
     .join(StudentProfile, StudentProfile.user_id == User.user_id) \
     .filter(AttendanceRecord.teacher_id == teacher.id)
    
    if selected_level:
        records_query = records_query.filter(StudentProfile.programme_level == int(selected_level))
    if selected_programme:
        records_query = records_query.filter(StudentProfile.current_programme == selected_programme)
    if selected_date:
        records_query = records_query.filter(AttendanceRecord.date == selected_date)
    
    records_query = records_query.order_by(AttendanceRecord.date)
    formatted_records = [{
        'date': r.date,
        'programme': r.current_programme,
        'level': r.programme_level,
        'full_name': " ".join(filter(None, [r.first_name, r.middle_name, r.last_name])),
        'is_present': r.is_present
    } for r in records_query.all()]

    # Get programme and level options for filters
    programme_options = db.session.query(StudentProfile.current_programme).distinct() \
        .order_by(StudentProfile.current_programme).all()
    programme_list = [p[0] for p in programme_options]

    level_options = db.session.query(StudentProfile.programme_level).distinct() \
        .order_by(StudentProfile.programme_level).all()
    level_list = sorted([l[0] for l in level_options if l[0]])

    # Student list for Excel view
    student_list = [{
        'id': s[0],
        'full_name': " ".join(filter(None, [s[1], s[2], s[3]])),
        'programme': s[4],
        'level': s[5]
    } for s in students]

    return render_template(
        'teacher/view_attendance.html',
        students=student_list,
        dates=date_list,
        attendance_map=attendance_map,
        programmes=programme_list,
        levels=level_list,
        selected_programme=selected_programme,
        selected_level=selected_level,
        selected_date=selected_date,
        records=formatted_records
    )
    
@teacher_bp.route('/calendar')
@login_required
def calendar():
    if current_user.role != 'teacher':
        abort(403)

    # Fetch all academic events: past, present, future
    events = AcademicCalendar.query.order_by(AcademicCalendar.date).all()

    # Map break_type to colors (can adjust to your preference)
    color_map = {
        'Vacation': '#e67e22',
        'Midterm': '#9b59b6',
        'Exam': '#2980b9',
        'Holiday': '#c0392b',
        'Other': '#95a5a6'
    }

    cal_events = []
    for e in events:
        cal_events.append({
            'id': e.id,
            'title': e.label,
            'start': e.date.isoformat(),
            'color': color_map.get(e.break_type, '#7f8c8d'),
            'backgroundColor': '#28a745' if e.is_workday else '#dc3545',
            'type': e.break_type,
            'display': 'auto'
        })

    # Semester background highlights
    academic_year = AcademicYear.query.first()
    if academic_year:
        if academic_year.semester_1_start and academic_year.semester_1_end:
            cal_events.append({
                'start': academic_year.semester_1_start.isoformat(),
                'end': (academic_year.semester_1_end + timedelta(days=1)).isoformat(),
                'display': 'background',
                'color': '#d1e7dd',
                'title': 'Semester 1'
            })
        if academic_year.semester_2_start and academic_year.semester_2_end:
            cal_events.append({
                'start': academic_year.semester_2_start.isoformat(),
                'end': (academic_year.semester_2_end + timedelta(days=1)).isoformat(),
                'display': 'background',
                'color': '#f8d7da',
                'title': 'Semester 2'
            })

    return render_template('teacher/calendar.html', cal_events=cal_events)

# Appointments Management
@teacher_bp.route('/appointment-slots', methods=['GET', 'POST'])
@login_required
def manage_slots():
    teacher = TeacherProfile.query.filter_by(user_id=current_user.user_id).first_or_404()

    # Delete expired slots (where end datetime is in the past)
    now = datetime.now()
    expired_slots = AppointmentSlot.query.filter(
        AppointmentSlot.teacher_id == teacher.id,
        db.func.datetime(AppointmentSlot.date, AppointmentSlot.end_time) < now,
        AppointmentSlot.is_booked == False  # Optional: only delete unbooked
    ).all()

    for slot in expired_slots:
        db.session.delete(slot)
    db.session.commit()

    if request.method == 'POST':
        date = request.form['date']
        start = request.form['start_time']
        end = request.form['end_time']
        slot = AppointmentSlot(
            teacher_id=teacher.id,
            date=datetime.strptime(date, '%Y-%m-%d').date(),
            start_time=datetime.strptime(start, '%H:%M').time(),
            end_time=datetime.strptime(end, '%H:%M').time()
        )
        db.session.add(slot)
        db.session.commit()
        flash('Slot added.')
        return redirect(url_for('teacher.manage_slots'))

    slots = AppointmentSlot.query.filter_by(teacher_id=teacher.id).all()
    return render_template('teacher/appointment_slots.html', slots=slots)

@teacher_bp.route('/appointment-requests')
@login_required
def appointment_requests():
    teacher = TeacherProfile.query.filter_by(user_id=current_user.user_id).first_or_404()
    slots = AppointmentSlot.query.filter_by(teacher_id=teacher.id).all()
    bookings = AppointmentBooking.query \
        .filter(AppointmentBooking.slot_id.in_([s.id for s in slots])) \
        .options(joinedload(AppointmentBooking.student).joinedload(StudentProfile.user)) \
        .all()
    return render_template('teacher/appointment_requests.html', bookings=bookings)


@teacher_bp.route('/appointment/update-status/<int:booking_id>/<string:status>')
@login_required
def update_booking_status(booking_id, status):
    booking = AppointmentBooking.query.get_or_404(booking_id)
    booking.status = status
    db.session.commit()
    flash(f'Booking marked as {status}')
    return redirect(url_for('teacher.appointment_requests'))

@teacher_bp.route("/slots/delete/<int:slot_id>", methods=["POST"])
@login_required
def delete_slot(slot_id):
    slot = AppointmentSlot.query.get_or_404(slot_id)

    if slot.is_booked:
        flash("Cannot delete a booked slot.", "danger")
    else:
        db.session.delete(slot)
        db.session.commit()
        flash("Slot deleted successfully.", "success")

    return redirect(url_for("teacher.manage_slots"))

# Then replace ALL instances of:
# teacher_only()
# 
# With:
# teacher_or_teacher_only()

# Here are all the routes that need updating:

    
# Reports
from sqlalchemy import distinct

@teacher_bp.route('/reports')
@login_required
def reports():
    # Fetch distinct programme + level combinations from students
    cohorts = (
        db.session.query(
            StudentProfile.current_programme,
            StudentProfile.programme_level
        )
        .distinct()
        .order_by(StudentProfile.current_programme, StudentProfile.programme_level)
        .all()
    )

    # Format as a list of strings for the template: "Programme Level"
    cohorts_list = [f"{p} {l}" for p, l in cohorts]

    # Fetch academic years
    years = AcademicYear.query.all()

    return render_template('teacher/reports.html', classes=cohorts_list, years=years)

@teacher_bp.route('/results/combined')
@login_required
def view_results_combined():
    """Show quiz/exam/assignment results"""
    teacher_profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    if not teacher_profile:
        return render_template('teacher/view_results_combined.html', results=[], courses=[], message="No teacher profile found.")

    assignments = TeacherCourseAssignment.query.filter_by(teacher_id=teacher_profile.id).all()
    course_ids = [a.course_id for a in assignments]
    if not course_ids:
        return render_template('teacher/view_results_combined.html', results=[], courses=[], message="No courses assigned.")

    courses = Course.query.filter(Course.id.in_(course_ids)).all()
    course_names = [c.name for c in courses]

    combined = []

    for course in courses:
        scheme = CourseAssessmentScheme.query.filter_by(course_id=course.id, teacher_id=teacher_profile.id).first()

        # ===== AGGREGATE QUIZZES BY STUDENT =====
        quiz_subs = (
            db.session.query(StudentQuizSubmission, Quiz, User)
            .join(Quiz, Quiz.id == StudentQuizSubmission.quiz_id)
            .join(User, User.id == StudentQuizSubmission.student_id)
            .filter(Quiz.course_id == course.id)
            .all()
        )

        # Group quizzes by student
        student_quizzes = {}
        for sub, quiz, user in quiz_subs:
            key = (user.id, user.first_name, user.last_name)
            if key not in student_quizzes:
                student_quizzes[key] = {'scores': [], 'max_scores': [], 'dates': []}
            student_quizzes[key]['scores'].append(float(getattr(sub, "score", 0) or 0))
            student_quizzes[key]['max_scores'].append(float(getattr(quiz, 'max_score', 0) or 0))
            student_quizzes[key]['dates'].append(getattr(sub, "submitted_at", None))

        # Create aggregated quiz entry per student
        for (user_id, first_name, last_name), data in student_quizzes.items():
            total_score = sum(data['scores'])
            total_max = sum(data['max_scores'])
            latest_date = max(data['dates']) if data['dates'] else None
            
            if scheme and total_max:
                weighted_score = (total_score / total_max) * scheme.quiz_weight
            else:
                weighted_score = None

            combined.append({
                "type": "Quiz",
                "course": course.name,
                "student": f"{first_name} {last_name}",
                "score": total_score,
                "date": latest_date,
                "raw_score": total_score,
                "weight_percent": scheme.quiz_weight if scheme else None,
                "weighted_score": weighted_score
            })

        # ===== AGGREGATE EXAMS BY STUDENT =====
        exam_subs = (
            db.session.query(ExamSubmission, Exam, User)
            .join(Exam, Exam.id == ExamSubmission.exam_id)
            .join(User, User.id == ExamSubmission.student_id)
            .filter(Exam.course_id == course.id)
            .all()
        )
        
        # Group exams by student
        student_exams = {}
        for sub, exam, user in exam_subs:
            key = (user.id, user.first_name, user.last_name)
            if key not in student_exams:
                student_exams[key] = {'scores': [], 'max_scores': [], 'dates': []}
            student_exams[key]['scores'].append(float(getattr(sub, "score", 0) or 0))
            student_exams[key]['max_scores'].append(float(getattr(exam, 'max_score', 0) or 0))
            student_exams[key]['dates'].append(getattr(sub, "submitted_at", None))

        # Create aggregated exam entry per student
        for (user_id, first_name, last_name), data in student_exams.items():
            total_score = sum(data['scores'])
            total_max = sum(data['max_scores'])
            latest_date = max(data['dates']) if data['dates'] else None
            
            exam_weight = scheme.exam_weight if scheme else 0.0
            if scheme and total_max and exam_weight > 0:
                weighted_score = (total_score / total_max) * exam_weight
            else:
                weighted_score = None
            combined.append({
                "type": "Exam",
                "course": course.name,
                "student": f"{first_name} {last_name}",
                "score": total_score,
                "date": latest_date,
                "raw_score": total_score,
                "weight_percent": exam_weight,
                "weighted_score": weighted_score
            })

        # ===== AGGREGATE ASSIGNMENTS BY STUDENT =====
        assignment_subs = (
            db.session.query(AssignmentSubmission, Assignment, User)
            .join(Assignment, Assignment.id == AssignmentSubmission.assignment_id)
            .join(User, User.id == AssignmentSubmission.student_id)
            .filter(Assignment.course_id == course.id)
            .filter(AssignmentSubmission.score != None)  # Only include scored assignments
            .all()
        )
        
        # Group assignments by student
        student_assignments = {}
        for sub, assignment, user in assignment_subs:
            key = (user.id, user.first_name, user.last_name)
            if key not in student_assignments:
                student_assignments[key] = {'scores': [], 'max_scores': [], 'dates': []}
            student_assignments[key]['scores'].append(float(getattr(sub, "score", 0) or 0))
            student_assignments[key]['max_scores'].append(float(getattr(assignment, 'max_score', 0) or 0))
            student_assignments[key]['dates'].append(getattr(sub, "submitted_at", None))

        # Create aggregated assignment entry per student
        for (user_id, first_name, last_name), data in student_assignments.items():
            total_score = sum(data['scores'])
            total_max = sum(data['max_scores'])
            latest_date = max(data['dates']) if data['dates'] else None
            
            assignment_weight = scheme.assignment_weight if scheme else 0.0
            if scheme and total_max and assignment_weight > 0:
                weighted_score = (total_score / total_max) * assignment_weight
            else:
                weighted_score = None
            combined.append({
                "type": "Assignment",
                "course": course.name,
                "student": f"{first_name} {last_name}",
                "score": total_score,
                "date": latest_date,
                "raw_score": total_score,
                "weight_percent": assignment_weight,
                "weighted_score": weighted_score
            })

    # Format dates
    for r in combined:
        if r["date"]:
            try:
                r["date"] = r["date"].strftime("%Y-%m-%d %H:%M")
            except Exception:
                r["date"] = str(r["date"])
        else:
            r["date"] = ""

    combined.sort(key=lambda r: r.get("date") or "", reverse=True)

    # Determine current academic year and whether semester has ended
    today = date.today()
    current_year_obj = AcademicYear.query.filter(
        AcademicYear.start_date <= today,
        AcademicYear.end_date >= today
    ).first()

    academic_year_str = None
    semester_for_check = None
    can_submit_vetting = False
    semester_status = {'is_locked': False, 'is_released': False}

    if current_year_obj:
        academic_year_str = f"{current_year_obj.start_date.year}/{current_year_obj.end_date.year}"

    # Use first course's semester as the target semester for vetting controls
    if courses:
        semester_for_check = courses[0].semester

    if academic_year_str and semester_for_check:
        # Determine if semester has ended based on AcademicYear fields
        try:
            s = str(semester_for_check).lower()
            if ('1' in s) or ('first' in s) or ('one' in s):
                sem_ended = current_year_obj.semester_1_end < today if current_year_obj and current_year_obj.semester_1_end else False
            elif ('2' in s) or ('second' in s) or ('two' in s):
                sem_ended = current_year_obj.semester_2_end < today if current_year_obj and current_year_obj.semester_2_end else False
            else:
                # Fallback: assume semester 1 if ambiguous
                sem_ended = current_year_obj.semester_1_end < today if current_year_obj and current_year_obj.semester_1_end else False
        except Exception:
            sem_ended = False

        # Get current semester status
        semester_status = SemesterGradingService.get_semester_status(academic_year_str, semester_for_check)

        # Allow submission for vetting only if semester ended and not already locked
        can_submit_vetting = sem_ended and not semester_status.get('is_locked', False)

    return render_template('teacher/view_results_combined.html', results=combined, courses=course_names, message=None,
                           can_submit_vetting=can_submit_vetting, academic_year=academic_year_str, semester=semester_for_check,
                           semester_status=semester_status)


@teacher_bp.route('/results/submit-for-vetting', methods=['POST'])
@login_required
def submit_for_vetting():
    """Submit semester results for admin vetting"""
    
    teacher_profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    if not teacher_profile:
        flash("Teacher profile not found.", "danger")
        return redirect(url_for('teacher.view_results_combined'))

    # Get parameters from form
    academic_year = configured_academic_year()
    semester = request.form.get('semester', '').strip()

    if not academic_year or not semester:
        flash("Academic year and semester are required.", "danger")
        return redirect(url_for('teacher.view_results_combined'))

    try:
        # Get all courses assigned to this teacher
        assignments = TeacherCourseAssignment.query.filter_by(teacher_id=teacher_profile.id).all()
        course_ids = [a.course_id for a in assignments]

        if not course_ids:
            flash("No courses assigned to you.", "warning")
            return redirect(url_for('teacher.view_results_combined'))

        # ✅ FIX: Get actual course objects and extract their IDs
        courses = Course.query.filter(Course.id.in_(course_ids)).all()
        
        # Create list of course data
        submitted_courses = []
        for course in courses:
            submitted_courses.append({
                'id': course.id,
                'code': course.code,
                'name': course.name
            })

        logger.info(f"Teacher {current_user.user_id} submitting {len(submitted_courses)} courses for vetting")
        logger.info(f"Submitted courses: {submitted_courses}")

        # Check if a release row exists for this academic year + semester
        existing_any = SemesterResultRelease.query.filter_by(
            academic_year=academic_year,
            semester=semester
        ).first()

        # If there's an existing locked (and not released) record, stop the submission
        if existing_any and existing_any.is_locked and not existing_any.is_released:
            flash("This semester is already locked for vetting.", "info")
            return redirect(url_for('teacher.view_results_combined'))

        # Upsert: update existing row when present (resubmission), otherwise create new
        if existing_any:
            # Update fields for resubmission
            existing_any.is_locked = True
            existing_any.submitted_courses = json.dumps(submitted_courses)
            existing_any.locked_at = datetime.utcnow()
            existing_any.updated_at = datetime.utcnow() if hasattr(existing_any, 'updated_at') else None
            release = existing_any
            logger.info(f"Updated existing semester release {academic_year} {semester} with {len(submitted_courses)} courses")
        else:
            # Create new submission
            release = SemesterResultRelease(
                academic_year=academic_year,
                semester=semester,
                is_locked=True,
                submitted_courses=json.dumps(submitted_courses),
                locked_at=datetime.utcnow()
            )
            db.session.add(release)
            logger.info(f"Submitted semester {academic_year} {semester} with {len(submitted_courses)} courses")

        db.session.commit()

        # Lock the semester in grading service too
        SemesterGradingService.lock_semester(academic_year, semester)

        flash(f"✓ Submitted {len(submitted_courses)} course(s) for vetting. Semester is now locked.", "success")
        return redirect(url_for('teacher.view_results_combined'))

    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error submitting for vetting: {e}")
        flash(f"Error submitting for vetting: {e}", "danger")
        return redirect(url_for('teacher.view_results_combined'))
    
@teacher_bp.route("/course/<int:course_id>/grading", methods=["GET", "POST"])
@login_required
def course_grading(course_id):
    if current_user.role != "teacher":
        abort(403)

    teacher = TeacherProfile.query.filter_by(user_id=current_user.user_id).first_or_404()
    course = Course.query.get_or_404(course_id)

    scheme = CourseAssessmentScheme.query.filter_by(
        course_id=course.id,
        teacher_id=teacher.id
    ).first()

    if request.method == "POST":
        quiz = float(request.form["quiz_weight"])
        assignment = float(request.form["assignment_weight"])
        exam = float(request.form["exam_weight"])

        if quiz + assignment + exam != 100:
            flash("Total weight must equal 100%", "danger")
            return redirect(request.url)

        if not scheme:
            scheme = CourseAssessmentScheme(
                course_id=course.id,
                teacher_id=teacher.id
            )
            db.session.add(scheme)

        scheme.quiz_weight = quiz
        scheme.assignment_weight = assignment
        scheme.exam_weight = exam

        db.session.commit()
        flash("Grading scheme saved", "success")

    return render_template(
        "teacher/course_grading.html",
        course=course,
        scheme=scheme
    )


# Profile
@teacher_bp.route('/profile')
@login_required
def profile():
    if not current_user.is_teacher:
        abort(403)

    profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    return render_template('teacher/profile.html', profile=profile)

@teacher_bp.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        # Verify current password
        if current_user.check_password(form.current_password.data):
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash('Password updated successfully!', 'success')
            return redirect(url_for('teacher.profile'))
        else:
            flash('Current password is incorrect.', 'danger')
    return render_template('teacher/change_password.html', form=form)

# -------------------------
# List all meetings for the teacher
# -------------------------
@teacher_bp.route('/meetings')
@login_required
def meetings():
    if current_user.role != 'teacher':
        abort(403)

    profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    if not profile:
        flash("Please complete your profile first.", "warning")
        return redirect(url_for('teacher.dashboard'))

    # Only meetings for courses this teacher is registered to
    meetings = Meeting.query.join(Course).filter(Course.id.in_([a.course_id for a in profile.assignments]))\
        .order_by(Meeting.scheduled_start.desc()).all()

    return render_template('teacher/meetings_list.html', meetings=meetings)


# -------------------------
# Add new meeting
# -------------------------
# -------------------------
# Zoom helpers
# -------------------------
import requests
from flask import current_app
from requests.auth import HTTPBasicAuth

def get_zoom_access_token():
    """
    Get access token from Zoom (Server-to-Server OAuth)
    """
    url = "https://zoom.us/oauth/token"

    client_id = current_app.config.get("ZOOM_CLIENT_ID")
    client_secret = current_app.config.get("ZOOM_CLIENT_SECRET")
    account_id = current_app.config.get("ZOOM_ACCOUNT_ID")

    if not client_id or not client_secret:
        raise Exception("Zoom client credentials are not configured (ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET)")

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # If an account_id is provided, try the account_credentials flow first
    if account_id:
        data = {"grant_type": "account_credentials", "account_id": account_id}
        resp = requests.post(url, data=data, auth=HTTPBasicAuth(client_id, client_secret), headers=headers)
        current_app.logger.info(f"Zoom token (account_credentials) response: {resp.status_code} - {resp.text}")
        if resp.status_code == 200:
            token_data = resp.json()
            current_app.logger.info(f"Zoom token keys: {list(token_data.keys())}")
            return token_data.get("access_token")

    # Fallback: standard client_credentials grant (Server-to-Server OAuth)
    data = {"grant_type": "client_credentials"}
    resp = requests.post(url, data=data, auth=HTTPBasicAuth(client_id, client_secret), headers=headers)
    current_app.logger.info(f"Zoom token (client_credentials) response: {resp.status_code} - {resp.text}")
    if resp.status_code != 200:
        current_app.logger.error(f"Zoom token error: {resp.status_code} - {resp.text}")
        raise Exception(f"Failed to get Zoom token: {resp.status_code} - {resp.text}")

    token_data = resp.json()
    current_app.logger.info(f"Zoom token keys: {list(token_data.keys())}")
    return token_data.get("access_token")

def create_zoom_meeting(topic, start_time, duration_min=60):
    token = get_zoom_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    body = {
        "topic": topic,
        "type": 2,
        "start_time": start_time.isoformat(),
        "duration": duration_min,
        "settings": {
            "join_before_host": True,
            "mute_upon_entry": True
        }
    }

    url = "https://api.zoom.us/v2/users/me/meetings"
    response = requests.post(url, headers=headers, json=body)

    if response.status_code == 201:
        return response.json()

    # Log details for debugging (do not log full token in production)
    token_preview = (token[:20] + "...") if token else "<no-token>"
    current_app.logger.error(
        f"Zoom meeting creation error: {response.status_code} - {response.text} - token_preview={token_preview}"
    )

    if response.status_code == 401:
        raise Exception(f"Failed to create Zoom meeting: {response.status_code} - {response.text}")

    response.raise_for_status()


# -------------------------
# Add meeting
# -------------------------
@teacher_bp.route("/meetings/add", methods=["GET", "POST"])
@login_required
def add_meeting():
    if current_user.role != "teacher":
        abort(403)

    profile = TeacherProfile.query.filter_by(user_id=current_user.user_id).first()
    form = MeetingForm()
    form.course_id.choices = [(a.course.id, a.course.name) for a in profile.assignments]

    if form.validate_on_submit():
        duration = int((form.scheduled_end.data - form.scheduled_start.data).total_seconds() // 60)
        zoom_meeting = create_zoom_meeting(form.title.data, form.scheduled_start.data, duration)

        meeting = Meeting(
            title=form.title.data,
            description=form.description.data,
            host_id=current_user.user_id,
            course_id=form.course_id.data,
            meeting_code=zoom_meeting["id"],
            scheduled_start=form.scheduled_start.data,
            scheduled_end=form.scheduled_end.data,
            join_url=zoom_meeting["join_url"],
            start_url=zoom_meeting["start_url"]
        )
        db.session.add(meeting)
        db.session.commit()
        flash("Zoom meeting created successfully!", "success")
        return redirect(url_for("teacher.meetings"))

    return render_template("teacher/meeting_form.html", form=form)


# Exams Management
@teacher_bp.route('/exam/<int:exam_id>/sets/create', methods=['GET', 'POST'])
@login_required
def create_exam_set(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    form = ExamSetForm()

    if form.validate_on_submit():
        try:
            name = form.name.data.strip()
            password = form.access_password.data.strip()

            new_set = ExamSet(
                name=name,
                exam_id=exam.id,
                access_password=password   # ✅ save the password
            )
            db.session.add(new_set)
            db.session.commit()
            flash(f"Set '{name}' created.", "success")
            return redirect(url_for('teacher.exam_sets', exam_id=exam.id))

        except Exception as e:
            current_app.logger.exception("Failed creating set")
            db.session.rollback()
            flash(f"Error creating set: {e}", "danger")
            return redirect(request.url)

    return render_template('teacher/create_exam_set.html', exam=exam, form=form)

@teacher_bp.route('/exam/<int:exam_id>/sets', methods=['GET'])
@login_required
def exam_sets(exam_id):
    exam = Exam.query.get_or_404(exam_id)

    pool_questions = ExamQuestion.query.filter_by(exam_id=exam.id).order_by(ExamQuestion.id).all()

    sets = ExamSet.query.filter_by(exam_id=exam.id).order_by(ExamSet.id).all()

    set_q_map = {}
    for s in sets:
        set_q_map[s.id] = [sq.question_id for sq in s.set_questions]

    return render_template(
        'teacher/exam_sets.html',
        exam=exam,
        pool_questions=pool_questions,
        sets=sets,
        set_q_map=set_q_map
    )

@teacher_bp.route('/exams')
@login_required
def manage_exams():
    exams = Exam.query.order_by(Exam.start_datetime.desc()).all()
    return render_template('teacher/manage_exams.html', exams=exams)

@teacher_bp.route('/exams/add', methods=['GET', 'POST'])
@login_required
def add_exam():
    """Create a new exam."""
    if current_user.role != 'teacher':
        abort(403)

    form = ExamForm()

    # Populate programme choices
    form.programme_name.choices = [
        ('', '— Select Programme —'),
        ('Cyber Security', 'Cyber Security'),
        ('Early Childhood Education', 'Early Childhood Education'),
        ('Dispensing Technician II & III', 'Dispensing Technician II & III'),
        ('Diagnostic Medical Sonography', 'Diagnostic Medical Sonography'),
        ('Medical Laboratory Technology', 'Medical Laboratory Technology'),
        ('Dispensing Assistant', 'Dispensing Assistant'),
        ('Health Information Management', 'Health Information Management'),
        ('Optical Technician', 'Optical Technician'),
        ('Midwifery', 'Midwifery'),
        ('Ophthalmic Dispensing', 'Ophthalmic Dispensing'),
        ('HND Dispensing Technology', 'HND Dispensing Technology'),
        ('Diploma in Early Childhood Education', 'Diploma in Early Childhood Education')
    ]

    # Populate level choices
    form.programme_level.choices = [
        ('', '— Select Level —'),
        ('100', 'Level 100'),
        ('200', 'Level 200'),
        ('300', 'Level 300'),
        ('400', 'Level 400')
    ]

    # ✅ POPULATE COURSE CHOICES BEFORE VALIDATION (for both GET and POST)
    selected_programme = request.form.get('programme_name', '') or form.programme_name.data or ''
    selected_level = request.form.get('programme_level', '') or form.programme_level.data or ''

    course_list = []
    if selected_programme and selected_level:
        course_list = Course.query.filter_by(
            programme_name=selected_programme,
            programme_level=selected_level
        ).order_by(Course.name).all()

    form.course_id.choices = [(0, '— Select Course —')] + [
        (c.id, f"{c.code} — {c.name}") for c in course_list
    ]

    # Handle GET request - just show the form
    if request.method == 'GET':
        return render_template('teacher/add_exam.html', form=form)

    # ============ HANDLE POST REQUEST ============
    print("\n" + "="*60)
    print("EXAM FORM SUBMISSION DEBUG")
    print("="*60)
    print(f"Form validates: {form.validate()}")
    if form.errors:
        print(f"Form Errors: {form.errors}")
        for field, errors in form.errors.items():
            flash(f"{field}: {', '.join(errors)}", "danger")
        return render_template('teacher/add_exam.html', form=form)

    print("Form is valid")
    print("="*60 + "\n")

    try:
        # Extract form data
        programme_name = form.programme_name.data.strip()
        programme_level = form.programme_level.data.strip()
        title = form.title.data.strip()
        course_id = form.course_id.data
        start_datetime = form.start_datetime.data
        end_datetime = form.end_datetime.data
        duration = int(form.duration_minutes.data) if form.duration_minutes.data else 60
        assignment_mode = form.assignment_mode.data
        assignment_seed = form.assignment_seed.data or None

        # Validate dates
        if start_datetime >= end_datetime:
            flash("Start date/time must be before end date/time.", "danger")
            return render_template('teacher/add_exam.html', form=form)

        # Verify course exists and belongs to selected programme/level
        course = None
        if course_id and int(course_id) != 0:
            try:
                course_id = int(course_id)
                course = Course.query.filter_by(
                    id=course_id,
                    programme_name=programme_name,
                    programme_level=programme_level
                ).first()
                
                if not course:
                    flash("Selected course not found or doesn't match the selected programme/level.", "danger")
                    return render_template('teacher/add_exam.html', form=form)
            except (ValueError, TypeError):
                flash("Invalid course selected.", "danger")
                return render_template('teacher/add_exam.html', form=form)
        else:
            flash("Please select a course.", "danger")
            return render_template('teacher/add_exam.html', form=form)

        # Check for duplicate exam
        dup_check = Exam.query.filter_by(
            title=title,
            course_id=course.id,
            programme_name=programme_name,
            programme_level=programme_level
        ).first()

        if dup_check:
            flash("An exam with this title already exists for this course.", "danger")
            return render_template('teacher/add_exam.html', form=form)

        # Create exam
        exam = Exam(
            title=title,
            course_id=course.id,
            programme_name=programme_name,
            programme_level=programme_level,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            duration_minutes=duration,
            assignment_mode=assignment_mode,
            assignment_seed=assignment_seed
        )

        db.session.add(exam)
        db.session.commit()

        print(f"Exam '{title}' created successfully")
        flash(f"✓ Exam '{title}' created successfully!", "success")
        return redirect(url_for('teacher.manage_exams'))

    except Exception as e:
        db.session.rollback()
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f"Error creating exam: {str(e)}", "danger")
        return render_template('teacher/add_exam.html', form=form)
    
@teacher_bp.route('/exam/<int:exam_id>/questions/create', methods=['GET', 'POST'])
@login_required
def create_exam_question(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    form = ExamQuestionForm()

    if form.validate_on_submit():
        try:
            # create question row
            q = ExamQuestion(
                exam_id=exam.id,
                question_text=form.question_text.data.strip(),
                question_type=form.question_type.data,
                marks=form.marks.data,
            )
            db.session.add(q)
            db.session.flush()  # get q.id before adding options

            qtype = q.question_type

            # Helper to interpret checkbox values as boolean
            def is_checked_value(v):
                # checkbox might be 'on' or 'y' or 'true' or '1' depending on frontend
                return v is not None and str(v).lower() in ('on', 'y', 'true', '1')

            # ---------- MCQ ----------
            if qtype == "mcq":
                # find all keys like options-<idx>-text
                option_entries = []
                for key, val in request.form.items():
                    m = re.match(r'^options-(\d+)-text$', key)
                    if not m:
                        continue
                    idx = int(m.group(1))
                    text = (val or "").strip()
                    if not text:
                        # skip empty option texts
                        continue
                    # read corresponding is_correct checkbox if present
                    is_correct_raw = request.form.get(f'options-{idx}-is_correct')
                    is_correct = is_checked_value(is_correct_raw)
                    option_entries.append((idx, text, bool(is_correct)))

                # If no options found from dynamic inputs, try WTForms fieldlist (fallback)
                if not option_entries and getattr(form, 'options', None):
                    # form.options is a FieldList of subforms (may be preset in some cases)
                    for sub in form.options.entries:
                        text = (getattr(sub.form, 'text').data or "").strip()
                        if not text:
                            continue
                        is_correct = bool(getattr(sub.form, 'is_correct').data)
                        # index unknown here; we append with incremental index
                        option_entries.append((len(option_entries), text, is_correct))

                # sort entries by numeric index to preserve order
                option_entries.sort(key=lambda t: t[0])

                if len(option_entries) < 2:
                    # optional: enforce at least 2 options
                    current_app.logger.warning("MCQ created with fewer than 2 options")
                    # continue anyway, or raise/flash depending on your policy
                # Persist options
                for _, text, is_corr in option_entries:
                    opt = ExamOption(question_id=q.id, text=text, is_correct=bool(is_corr))
                    db.session.add(opt)

            # ---------- TRUE / FALSE ----------
            elif qtype == "true_false":
                # Prefer explicit options posted by JS: options-tf-0-text etc
                if any(k.startswith('options-tf-') for k in request.form.keys()):
                    # collect options-tf-<n>-text and their is_correct flags
                    tf_options = []
                    for key, val in request.form.items():
                        m = re.match(r'^options-tf-(\d+)-text$', key)
                        if not m:
                            continue
                        idx = int(m.group(1))
                        text = (val or "").strip()
                        if not text:
                            continue
                        is_correct = is_checked_value(request.form.get(f'options-tf-{idx}-is_correct'))
                        tf_options.append((idx, text, is_correct))
                    tf_options.sort(key=lambda t: t[0])
                    for _, text, is_corr in tf_options:
                        db.session.add(ExamOption(question_id=q.id, text=text, is_correct=bool(is_corr)))
                else:
                    # fallback: use radio tf_correct (value 'true' or 'false')
                    choice = request.form.get('tf_correct', 'true')
                    db.session.add(ExamOption(question_id=q.id, text='True', is_correct=(choice == 'true')))
                    db.session.add(ExamOption(question_id=q.id, text='False', is_correct=(choice == 'false')))

            # ---------- MATH (numeric answers) ----------
            elif qtype == "math":
                # collect math_answer-<idx> inputs
                math_answers = []
                for key, val in request.form.items():
                    m = re.match(r'^math_answer-(\d+)$', key)
                    if not m:
                        continue
                    idx = int(m.group(1))
                    raw = (val or "").strip()
                    if raw == '':
                        continue
                    # store as option text — mark as correct (we treat numeric answers as correct values)
                    math_answers.append((idx, raw))
                math_answers.sort(key=lambda t: t[0])
                for _, ans in math_answers:
                    db.session.add(ExamOption(question_id=q.id, text=ans, is_correct=True))

            # ---------- SUBJECTIVE ----------
            elif qtype == "subjective":
                # optional rubric/expected answer posted as 'subjective_rubric'
                rubric = (request.form.get('subjective_rubric') or "").strip()
                if rubric:
                    # store rubric as a non-correct option (so graders can see)
                    db.session.add(ExamOption(question_id=q.id, text=rubric, is_correct=False))
                # no student-selectable options to create

            # commit everything
            db.session.commit()
            flash("Question created.", "success")
            return redirect(url_for('teacher.exam_sets', exam_id=exam.id))

        except Exception as e:
            current_app.logger.exception("Failed creating question")
            db.session.rollback()
            flash(f"Error creating question: {e}", "danger")
            return redirect(request.url)

    # GET or form not valid: render template (the template you already have)
    return render_template('teacher/create_exam_question.html', exam=exam, form=form)

@teacher_bp.route('/exam/<int:exam_id>/sets/<int:set_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_exam_set(exam_id, set_id):
    exam = Exam.query.get_or_404(exam_id)
    exam_set = ExamSet.query.filter_by(id=set_id, exam_id=exam.id).first_or_404()
    form = ExamSetForm(obj=exam_set)

    if form.validate_on_submit():
        try:
            exam_set.name = form.name.data.strip()
            exam_set.access_password = form.access_password.data.strip()
            db.session.commit()
            flash("Set updated.", "success")
            return redirect(url_for('teacher.exam_sets', exam_id=exam.id))
        except Exception as e:
            current_app.logger.exception("Failed updating set")
            db.session.rollback()
            flash(f"Error: {e}", "danger")
            return redirect(request.url)

    # Questions in set
    set_questions = (
        db.session.query(ExamQuestion, ExamSetQuestion)
        .join(ExamSetQuestion, ExamQuestion.id == ExamSetQuestion.question_id)
        .filter(ExamSetQuestion.set_id == exam_set.id)
        .order_by(asc(ExamSetQuestion.order))
        .all()
    )
    set_question_list = [q for q, sq in set_questions]

    # Available pool
    pool_questions = ExamQuestion.query.filter_by(exam_id=exam.id).all()
    pool_ids = {q.id for q in set_question_list}
    available_questions = [q for q in pool_questions if q.id not in pool_ids]

    return render_template(
        'teacher/edit_exam_set.html',
        exam=exam,
        exam_set=exam_set,
        form=form,
        set_questions=set_question_list,
        available_questions=available_questions
    )

@teacher_bp.route("/edit_exam/<int:exam_id>", methods=["GET", "POST"])
@login_required
def edit_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    form = ExamForm(obj=exam)
    
    # Populate programme choices (hardcoded list like in add_quiz)
    form.programme_name.choices = [
        ('', '— Select Programme —'),
        ('Cyber Security', 'Cyber Security'),
        ('Early Childhood Education', 'Early Childhood Education'),
        ('Dispensing Technician II & III', 'Dispensing Technician II & III'),
        ('Diagnostic Medical Sonography', 'Diagnostic Medical Sonography'),
        ('Medical Laboratory Technology', 'Medical Laboratory Technology'),
        ('Dispensing Assistant', 'Dispensing Assistant'),
        ('Health Information Management', 'Health Information Management'),
        ('Optical Technician', 'Optical Technician'),
        ('Midwifery', 'Midwifery'),
        ('Ophthalmic Dispensing', 'Ophthalmic Dispensing'),
        ('HND Dispensing Technology', 'HND Dispensing Technology'),
        ('Diploma in Early Childhood Education', 'Diploma in Early Childhood Education')
    ]
    
    # Populate level choices
    form.programme_level.choices = [
        ('', '— Select Level —'),
        ('100', 'Level 100'),
        ('200', 'Level 200'),
        ('300', 'Level 300'),
        ('400', 'Level 400')
    ]
    
    # Get selected values from form data (POST) or form object
    selected_programme = request.form.get('programme_name', '') or form.programme_name.data or ''
    selected_level = request.form.get('programme_level', '') or form.programme_level.data or ''
    
    # Load courses based on programme + level
    course_list = []
    if selected_programme and selected_level:
        course_list = Course.query.filter_by(
            programme_name=selected_programme,
            programme_level=selected_level
        ).order_by(Course.name).all()

    form.course_id.choices = [(0, '— Select Course —')] + [
        (c.id, f"{c.code} — {c.name}") for c in course_list
    ]
    
    if form.validate_on_submit():
        exam.title = form.title.data.strip()
        exam.programme_name = form.programme_name.data
        exam.programme_level = form.programme_level.data
        exam.course_id = form.course_id.data
        exam.start_datetime = form.start_datetime.data
        exam.end_datetime = form.end_datetime.data
        exam.duration_minutes = form.duration_minutes.data
        exam.assignment_mode = form.assignment_mode.data
        exam.assignment_seed = (form.assignment_seed.data or None)
        db.session.commit()
        flash("Exam updated successfully!", "success")
        return redirect(url_for("teacher.manage_exams"))

    return render_template("teacher/edit_exam.html", form=form, exam=exam)

# ===============================
# Edit Exam Question
# ===============================
@teacher_bp.route('/exams/<int:exam_id>/questions/<int:question_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_exam_question(exam_id, question_id):
    exam = Exam.query.get_or_404(exam_id)
    question = ExamQuestion.query.get_or_404(question_id)

    form = ExamQuestionForm(obj=question)

    # Helper to interpret checkbox/posted values as boolean
    def is_checked_value(v):
        return v is not None and str(v).lower() in ('on', 'y', 'true', '1')

    # Helper to fetch and format question options
    def get_question_options():
        options = []
        math_answers = []
        tf_choice = None
        rubric = ""
        opts = ExamOption.query.filter_by(question_id=question.id).all()
        
        if question.question_type == 'mcq':
            for o in opts:
                options.append({'text': o.text, 'is_correct': bool(o.is_correct)})
        elif question.question_type == 'true_false':
            for o in opts:
                if o.text.lower().startswith('true'):
                    if o.is_correct:
                        tf_choice = 'true'
                if o.text.lower().startswith('false'):
                    if o.is_correct:
                        tf_choice = 'false'
            if tf_choice is None:
                tf_choice = 'true'
        elif question.question_type == 'math':
            for o in opts:
                math_answers.append(o.text)
        elif question.question_type == 'subjective':
            if opts:
                rubric = opts[0].text or ""
        
        return options, math_answers, tf_choice, rubric

    # Helper to render edit form
    def render_edit_form():
        options, math_answers, tf_choice, rubric = get_question_options()
        return render_template(
            "teacher/edit_exam_question.html",
            form=form,
            exam=exam,
            question=question,
            initial_options=options,
            initial_math=math_answers,
            tf_choice=(tf_choice or 'true'),
            rubric=rubric
        )

    if form.validate_on_submit():
        try:
            # Update basic fields
            question.question_text = form.question_text.data.strip()
            question.question_type = form.question_type.data
            question.marks = form.marks.data
            db.session.commit()

            # Remove existing options (we will recreate from posted form)
            ExamOption.query.filter_by(question_id=question.id).delete()
            db.session.flush()

            qtype = question.question_type

            # ---------- MCQ ----------
            if qtype == "mcq":
                option_entries = []
                for key, val in request.form.items():
                    m = re.match(r'^options-(\d+)-text$', key)
                    if not m:
                        continue
                    idx = int(m.group(1))
                    text = (val or "").strip()
                    if not text:
                        continue
                    is_correct_raw = request.form.get(f'options-{idx}-is_correct')
                    is_correct = is_checked_value(is_correct_raw)
                    option_entries.append((idx, text, bool(is_correct)))

                if not option_entries and getattr(form, 'options', None):
                    for sub in form.options.entries:
                        text = (getattr(sub.form, 'text').data or "").strip()
                        if not text:
                            continue
                        is_correct = bool(getattr(sub.form, 'is_correct').data)
                        option_entries.append((len(option_entries), text, is_correct))

                option_entries.sort(key=lambda t: t[0])
                for _, text, is_corr in option_entries:
                    db.session.add(ExamOption(question_id=question.id, text=text, is_correct=bool(is_corr)))

            # ---------- TRUE / FALSE ----------
            elif qtype == "true_false":
                if any(k.startswith('options-tf-') for k in request.form.keys()):
                    tf_options = []
                    for key, val in request.form.items():
                        m = re.match(r'^options-tf-(\d+)-text$', key)
                        if not m:
                            continue
                        idx = int(m.group(1))
                        text = (val or "").strip()
                        if not text:
                            continue
                        is_correct = is_checked_value(request.form.get(f'options-tf-{idx}-is_correct'))
                        tf_options.append((idx, text, is_correct))
                    tf_options.sort(key=lambda t: t[0])
                    for _, text, is_corr in tf_options:
                        db.session.add(ExamOption(question_id=question.id, text=text, is_correct=bool(is_corr)))
                else:
                    choice = request.form.get('tf_correct', 'true')
                    db.session.add(ExamOption(question_id=question.id, text='True', is_correct=(choice == 'true')))
                    db.session.add(ExamOption(question_id=question.id, text='False', is_correct=(choice == 'false')))

            # ---------- MATH (numeric answers) ----------
            elif qtype == "math":
                math_answers = []
                for key, val in request.form.items():
                    m = re.match(r'^math_answer-(\d+)$', key)
                    if not m:
                        continue
                    idx = int(m.group(1))
                    raw = (val or "").strip()
                    if raw == '':
                        continue
                    math_answers.append((idx, raw))
                math_answers.sort(key=lambda t: t[0])
                for _, ans in math_answers:
                    db.session.add(ExamOption(question_id=question.id, text=ans, is_correct=True))

            # ---------- SUBJECTIVE ----------
            elif qtype == "subjective":
                rubric = (request.form.get('subjective_rubric') or "").strip()
                if rubric:
                    db.session.add(ExamOption(question_id=question.id, text=rubric, is_correct=False))

            db.session.commit()
            flash("Question updated successfully!", "success")
            return redirect(url_for('teacher.exam_sets', exam_id=exam.id))

        except Exception as e:
            current_app.logger.exception("Failed updating question")
            db.session.rollback()
            flash(f"Error updating question: {e}", "danger")
            return render_edit_form()

    # GET or validation failed - render with existing options
    return render_edit_form()

# ===============================
# Delete Exam Question
# ===============================
@teacher_bp.route('/exams/<int:exam_id>/questions/<int:question_id>/delete', methods=['POST'])
@login_required
def delete_exam_question(exam_id, question_id):
    exam = Exam.query.get_or_404(exam_id)
    question = ExamQuestion.query.get_or_404(question_id)

    try:
        db.session.delete(question)
        db.session.commit()
        flash("Question deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting question: {str(e)}", "danger")

    return redirect(url_for('teacher.exam_sets', exam_id=exam.id))

@teacher_bp.route('/exams/delete/<int:exam_id>', methods=['POST', 'GET'])
@login_required
def delete_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)

    try:
        db.session.delete(exam)
        db.session.commit()
        flash("Exam deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Failed to delete exam")
        flash(f"Error deleting exam: {str(e)}", "danger")

    return redirect(url_for('teacher.manage_exams'))

# 4. Delete a set
@teacher_bp.route('/exam/<int:exam_id>/sets/<int:set_id>/delete', methods=['POST'])
@login_required
def delete_exam_set(exam_id, set_id):
    exam = Exam.query.get_or_404(exam_id)
    exam_set = ExamSet.query.filter_by(id=set_id, exam_id=exam.id).first_or_404()
    try:
        db.session.delete(exam_set)
        db.session.commit()
        flash("Set deleted.", "success")
    except Exception as e:
        current_app.logger.exception("Failed deleting set")
        db.session.rollback()
        flash(f"Error deleting set: {e}", "danger")
    return redirect(url_for('teacher.exam_sets', exam_id=exam.id))


# ---------- AJAX / API endpoints (use these from JS) ----------

# Add one or many questions to a set (POST JSON: {"question_ids":[1,2,3]})
@teacher_bp.route('/exam/<int:exam_id>/sets/<int:set_id>/add_questions', methods=['POST'])
@login_required
def add_questions_to_set(exam_id, set_id):
    exam = Exam.query.get_or_404(exam_id)
    exam_set = ExamSet.query.filter_by(id=set_id, exam_id=exam.id).first_or_404()

    payload = request.get_json() or {}
    question_ids = payload.get('question_ids') or []

    if not isinstance(question_ids, list):
        return jsonify({"status": "error", "message": "question_ids must be a list"}), 400

    added = []
    skipped = []
    try:
        for qid in question_ids:
            q = ExamQuestion.query.filter_by(id=int(qid), exam_id=exam.id).first()
            if not q:
                skipped.append({"id": qid, "reason": "question not found or belongs to another exam"})
                continue

            # skip if already in set
            exists = ExamSetQuestion.query.filter_by(set_id=exam_set.id, question_id=q.id).first()
            if exists:
                skipped.append({"id": qid, "reason": "already in set"})
                continue

            # determine order: append to end
            max_order_row = db.session.query(db.func.max(ExamSetQuestion.order)).filter_by(set_id=exam_set.id).scalar()
            next_order = (max_order_row or 0) + 1

            sq = ExamSetQuestion(set_id=exam_set.id, question_id=q.id, order=next_order)
            db.session.add(sq)
            added.append(qid)

        db.session.commit()
        return jsonify({"status": "ok", "added": added, "skipped": skipped}), 200

    except Exception as e:
        current_app.logger.exception("Failed adding questions to set")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


# Remove a question from a set
@teacher_bp.route('/exam/<int:exam_id>/sets/<int:set_id>/remove_question', methods=['POST'])
@login_required
def remove_question_from_set(exam_id, set_id):
    exam = Exam.query.get_or_404(exam_id)
    exam_set = ExamSet.query.filter_by(id=set_id, exam_id=exam.id).first_or_404()

    payload = request.get_json() or {}
    qid = payload.get('question_id')
    if not qid:
        return jsonify({"status": "error", "message": "question_id required"}), 400

    try:
        sq = ExamSetQuestion.query.filter_by(set_id=exam_set.id, question_id=int(qid)).first()
        if not sq:
            return jsonify({"status": "error", "message": "not found in set"}), 404

        db.session.delete(sq)
        db.session.commit()
        return jsonify({"status": "ok", "removed": qid}), 200
    except Exception as e:
        current_app.logger.exception("Failed removing question from set")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


# Reorder questions in a set (POST JSON: {"order":[qid1, qid2, qid3]})
@teacher_bp.route('/exam/<int:exam_id>/sets/<int:set_id>/reorder', methods=['POST'])
@login_required
def reorder_set_questions(exam_id, set_id):
    exam = Exam.query.get_or_404(exam_id)
    exam_set = ExamSet.query.filter_by(id=set_id, exam_id=exam.id).first_or_404()

    payload = request.get_json() or {}
    order_list = payload.get('order') or []
    if not isinstance(order_list, list):
        return jsonify({"status": "error", "message": "order must be a list"}), 400

    try:
        # Simple pass over list and update order value
        for idx, qid in enumerate(order_list, start=1):
            sq = ExamSetQuestion.query.filter_by(set_id=exam_set.id, question_id=int(qid)).first()
            if not sq:
                # skip silently or return error
                continue
            sq.order = idx
            db.session.add(sq)
        db.session.commit()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        current_app.logger.exception("Failed reordering set")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


@teacher_bp.route('/exam-timetable', methods=['GET'])
@login_required
def view_exam_timetable():
    """View exam timetable entries created by admin."""
    if current_user.role != 'teacher':
        abort(403)

    from models import ExamTimetableEntry
    from utils.helpers import get_programme_choices, get_level_choices

    # Fetch all exam timetable entries
    entries = ExamTimetableEntry.query.order_by(
        ExamTimetableEntry.date.asc(),
        ExamTimetableEntry.start_time.asc()
    ).all()

    # Fetch available programmes, levels, semesters for filtering
    programme_choices = get_programme_choices() or []
    programmes = [p for p, _ in programme_choices]

    level_choices = get_level_choices() or []
    levels = [l for l, _ in level_choices]

    # Fetch semesters from Course model
    semesters_q = db.session.query(Course.semester).distinct().order_by(Course.semester).all()
    semesters = [s[0] for s in semesters_q if s and s[0]]

    # Fetch courses for data mapping
    courses_q = Course.query.order_by(Course.name).all()
    courses_map = {}
    for c in courses_q:
        courses_map[c.code] = {
            'name': c.name,
            'programme_name': c.programme_name,
            'programme_level': c.programme_level,
            'semester': c.semester
        }

    return render_template('teacher/exam_timetable.html', entries=entries,
                           programmes=programmes, levels=levels,
                           semesters=semesters, courses_map=courses_map)


@teacher_bp.route('/assessment-performance')
@login_required
def assessment_performance():
    """Show teacher their assessment performance from students."""
    if current_user.role != 'teacher':
        abort(403)
    
    teacher = TeacherProfile.query.filter_by(user_id=current_user.user_id).first_or_404()
    
    # Get all assessments for this teacher
    assessments = TeacherAssessment.query.filter_by(teacher_id=current_user.user_id).all()
    
    if not assessments:
        return render_template('teacher/assessment_performance.html', 
                          teacher=teacher, 
                          assessments=[], 
                          stats=None,
                          periods=[])
    
    # Calculate statistics
    total_assessments = len(assessments)
    unique_students = len(set(a.student_id for a in assessments))
    
    # Get all answers for this teacher
    assessment_ids = [a.id for a in assessments]
    answers = TeacherAssessmentAnswer.query.filter(
        TeacherAssessmentAnswer.assessment_id.in_(assessment_ids)
    ).all()
    
    # Calculate average rating
    avg_rating = 0
    if answers:
        ratings = [float(answer.score) for answer in answers if hasattr(answer, 'score') and answer.score]
        avg_rating = sum(ratings) / len(ratings) if ratings else 0
    
    # Group by period
    period_stats = {}
    for assessment in assessments:
        period_id = assessment.period_id
        if period_id not in period_stats:
            period_stats[period_id] = {
                'period': None,
                'assessments': 0,
                'avg_rating': 0,
                'answers': []
            }
        period_stats[period_id]['assessments'] += 1
        period_stats[period_id]['answers'].extend([a for a in answers if a.assessment_id == assessment.id])
    
    # Calculate period averages and get period details
    periods = []
    for period_id, stats in period_stats.items():
        period = TeacherAssessmentPeriod.query.get(period_id)
        if period:
            stats['period'] = period
            period_ratings = [float(a.score) for a in stats['answers'] if hasattr(a, 'score') and a.score]
            stats['avg_rating'] = sum(period_ratings) / len(period_ratings) if period_ratings else 0
            periods.append(stats)
    
    # Overall stats
    overall_stats = {
        'total_assessments': total_assessments,
        'unique_students': unique_students,
        'avg_rating': avg_rating,
        'periods_count': len(periods)
    }
    
    return render_template('teacher/assessment_performance.html', 
                      teacher=teacher, 
                      assessments=assessments, 
                      stats=overall_stats,
                      periods=periods)
