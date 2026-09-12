from flask import Blueprint, current_app, render_template, abort, redirect, url_for, flash, jsonify, session, send_from_directory, send_file
import json, os, mimetypes
from flask import request
from flask_login import login_required, current_user, login_user, logout_user
from sqlalchemy import func, text, inspect
from werkzeug.utils import safe_join, secure_filename
from models import QuizAttempt, db, User, Quiz, StudentQuizSubmission, Question, StudentProfile, Assignment, CourseMaterial, StudentCourseRegistration, Course,  TimetableEntry, AcademicCalendar, AcademicYear, AppointmentSlot, AppointmentBooking, StudentFeeBalance, ProgrammeFeeStructure, StudentFeeTransaction, Exam, ExamSubmission, ExamQuestion, ExamAttempt, ExamSet, ExamSetQuestion, Meeting, StudentAnswer, Recording, PasswordResetRequest, PasswordResetToken, AssignmentSubmission, Conversation, ConversationParticipant
from datetime import date, datetime, timedelta, time
from forms import StudentLoginForm, ForgotPasswordForm, ResetPasswordForm
from io import BytesIO
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle
from utils.email import send_password_reset_email
from sqlalchemy.orm import joinedload
from flask_wtf.csrf import generate_csrf
from utils.agora import build_rtc_token, build_whiteboard_room_token, create_whiteboard_room

vclass_bp = Blueprint('vclass', __name__, url_prefix='/vclass')

ALLOWED_EXTENSIONS = {'.doc', '.docx', '.xls', '.xlsx', '.pdf', '.ppt', '.txt'}
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads", "assignments")

def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def ensure_meeting_class_conversation(meeting):
    """Ensure a persistent class conversation exists for the meeting and includes all enrolled students."""
    conv = None
    for candidate in Conversation.query.filter_by(type='class').all():
        meta = candidate.get_meta() or {}
        if meta.get('meeting_id') == meeting.id:
            conv = candidate
            break

    if conv is None:
        host_user = User.query.get(meeting.host_id)
        conv = Conversation(type='class')
        conv.set_meta({
            'name': meeting.title,
            'created_by': host_user.public_id if host_user else None,
            'meeting_id': meeting.id,
            'course_id': meeting.course_id,
        })
        db.session.add(conv)
        db.session.flush()

    def add_participant(user_obj, role='student'):
        if not user_obj or not getattr(user_obj, 'public_id', None):
            return
        existing = ConversationParticipant.query.filter_by(
            conversation_id=conv.id,
            user_public_id=user_obj.public_id,
        ).first()
        if existing:
            if role and existing.user_role != role:
                existing.user_role = role
            return
        db.session.add(ConversationParticipant(
            conversation_id=conv.id,
            user_public_id=user_obj.public_id,
            user_role=role,
            is_group_admin=(role == 'teacher' and user_obj.id == meeting.host_id)
        ))

    host_user = User.query.get(meeting.host_id)
    if host_user:
        add_participant(host_user, 'teacher')

    registrations = StudentCourseRegistration.query.filter_by(course_id=meeting.course_id).all()
    for registration in registrations:
        student = registration.student
        if student and student.role == 'student':
            add_participant(student, 'student')

    conv.updated_at = datetime.utcnow()
    db.session.commit()
    return conv

# Utility to split multi-day events into single-day all-day events
def split_event_into_days(title, start, end, color, extended_props):
    """Split a multi-day event into separate all-day blocks, one per day."""
    events = []
    current = start.date()
    final = end.date()
    while current <= final:
        next_day = current + timedelta(days=1)
        events.append({
            'title': title,
            'start': current.isoformat(),   # Date only for all-day events
            'end': next_day.isoformat(),    # Next day (non-inclusive end)
            'color': color,
            'allDay': True,
            'extendedProps': extended_props
        })
        current = next_day
    return events


@vclass_bp.route('/login', methods=['GET', 'POST'])
def vclass_login():
    form = StudentLoginForm()
    next_page = request.args.get('next')

    if form.validate_on_submit():
        username = form.username.data.strip()
        user_id = form.user_id.data.strip()
        password = form.password.data.strip()

        user = User.query.filter_by(user_id=user_id, role='student').first()
        if user and user.username.lower() == username.lower() and user.check_password(password):
            login_user(user)
            flash(f"Welcome back, {user.first_name}!", 'success')
            return redirect(next_page or url_for('vclass.dashboard'))

        flash('Invalid login credentials.', 'danger')

    return render_template('vclass/vclass_login.html', form=form)


@vclass_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user_id = form.user_id.data.strip() or None

        query = User.query.filter(db.func.lower(User.email) == email)
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        try:
            user = query.first()
        except Exception as e:
            current_app.logger.error(f"Database error in forgot_password: {e}")
            flash('A temporary error occurred. Please try again in a few moments.', 'warning')
            return redirect(url_for('select_portal'))

        if not user:
            flash('If that account exists, a reset email will be sent.', 'info')
            return redirect(url_for('auth.forgot_password'))

        # Log request
        reset_request = PasswordResetRequest(user_id=user.user_id, role=user.role)
        db.session.add(reset_request)
        db.session.commit()

        # Generate token
        token = PasswordResetToken.generate_for_user(user, request_obj=reset_request)

        # Send email immediately
        try:
            send_password_reset_email(user, token)
            reset_request.status = 'emailed'
            reset_request.email_sent_at = datetime.utcnow()
        except Exception as e:
            reset_request.status = 'email_failed'
            current_app.logger.exception(f"Failed to send password reset email: {e}")

        db.session.commit()

        flash('If your email exists, you’ll get a reset link shortly.', 'info')
        return redirect(url_for('select_portal'))

    return render_template('forgot_password.html', form=form)


@vclass_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    prt, status = PasswordResetToken.verify(token)
    if status != 'ok':
        messages = {
            'expired': 'Reset link expired.',
            'used': 'Reset link already used.',
            'invalid': 'Invalid reset link.'
        }
        flash(messages.get(status, 'danger'))
        return redirect(url_for('auth.forgot_password'))
    
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = prt.user
        user.set_password(form.password.data)
        prt.used = True
        prt.used_at = datetime.utcnow()
        if prt.request:
            prt.request.status = 'completed'
            prt.request.completed_at = datetime.utcnow()
        db.session.commit()
        flash('Password updated. Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', form=form)

@vclass_bp.route('/switch-to-vclass')
@login_required
def switch_to_vclass():
    logout_user()
    flash('You have been logged out. Please log in to access Virtual Class.', 'info')
    return redirect(url_for('vclass.vclass_login', next=url_for('vclass.dashboard')))


@vclass_bp.route('/switch-to-student-portal')
@login_required
def switch_to_student_portal():
    logout_user()
    flash('You have been logged out. Please log in to access the Student Portal.', 'info')
    return redirect(url_for('student.student_login', next=url_for('student.dashboard')))

@vclass_bp.route('/switch-to-student-courses')
@login_required
def switch_to_student_courses():
    logout_user()
    flash('You have been logged out. Please log in to access Course Registration.', 'info')
    return redirect(url_for('student.student_login', next=url_for('student.register_courses')))

# Virtual Classroom Dashboard
@vclass_bp.route('/dashboard')
@login_required
def dashboard():
    """Virtual classroom dashboard for tertiary students"""
    if current_user.role != 'student':
        abort(403)

    profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()
    if not profile:
        flash("Student profile not found.", "danger")
        return redirect(url_for("vclass.vclass_dashboard"))

    # Use programme and level instead of class
    student_programme = profile.current_programme
    student_level = str(profile.programme_level)
    now = datetime.utcnow()

    # --- Quizzes ---
    quizzes = Quiz.query.filter(
        Quiz.programme_name == student_programme,
        Quiz.programme_level == student_level
    ).all()
    
    quiz_list = []
    for q in quizzes:
        status = 'Upcoming'
        if q.start_datetime <= now <= q.end_datetime:
            status = 'Ongoing'
        elif now > q.end_datetime:
            status = 'Due'
        quiz_list.append({
            'id': q.id,
            'title': q.title,
            'course_name': q.course_name,
            'start_datetime': q.start_datetime.isoformat(),
            'end_datetime': q.end_datetime.isoformat(),
            'duration': q.duration_minutes,
            'is_active': q.start_datetime <= now <= q.end_datetime
        })

    # --- Assignments ---
    assignments = Assignment.query.filter(
        Assignment.programme_name == student_programme,
        Assignment.programme_level == student_level
    ).all()
    
    assignment_list = [{
        'id': a.id,
        'title': a.title,
        'course_name': a.course_name,
        'description': a.description,
        'instructions': a.instructions,
        'due_date': a.due_date.isoformat(),
        'filename': a.filename,
        'original_name': a.original_name
    } for a in assignments]

    # --- Course Materials ---
    materials = CourseMaterial.query.filter(
        CourseMaterial.programme_name == student_programme,
        CourseMaterial.programme_level == student_level
    ).all()
    
    material_list = [{
        'id': m.id,
        'title': m.title,
        'course_name': m.course_name,
        'filename': m.filename,
        'original_name': m.original_name,
        'file_type': m.file_type,
        'upload_date': m.upload_date.isoformat() if m.upload_date else None
    } for m in materials]

    # --- Combined Calendar Events ---
    events = []

    # Quizzes → Calendar
    for q in quizzes:
        status = 'Upcoming'
        color = '#0d6efd'
        if q.start_datetime <= now <= q.end_datetime:
            status = 'Ongoing'
            color = '#ffc107'
        elif now > q.end_datetime:
            status = 'Due'
            color = '#dc3545'

        events.append({
            'title': f"{q.title} [{status}]",
            'start': q.start_datetime.isoformat(),
            'end': q.end_datetime.isoformat(),
            'url': url_for('vclass.quiz_instructions', quiz_id=q.id),
            'color': color,
            'extendedProps': {
                'type': 'Quiz',
                'status': status,
                'course': q.course_name,
                'description': ''
            }
        })

    # Assignments → Calendar
    for a in assignments:
        events.append({
            'title': f"{a.title} [Due]",
            'start': a.due_date.isoformat(),
            'end': a.due_date.isoformat(),
            'url': url_for('vclass.download_assignment', filename=a.filename),
            'color': '#198754',
            'extendedProps': {
                'type': 'Assignment',
                'status': 'Due',
                'course': a.course_name,
                'description': a.instructions or ''
            }
        })

    # Course Registration Period
    registration_start, registration_end = Course.get_registration_window()
    if registration_start and registration_end:
        events += split_event_into_days(
            title='Course Registration Period',
            start=registration_start,
            end=registration_end,
            color='#dc3545',
            extended_props={
                'type': 'Deadline',
                'status': 'Open',
                'course': '',
                'description': 'Course registration is available during this window.'
            }
        )

    # Academic Calendar Events
    ac_events = AcademicCalendar.query.order_by(AcademicCalendar.date).all()
    color_map = {
        'Vacation': '#e67e22',
        'Midterm': '#9b59b6',
        'Exam': '#2980b9',
        'Holiday': '#c0392b',
        'Other': '#95a5a6'
    }

    for ev in ac_events:
        events.append({
            'title': ev.label,
            'start': ev.date.isoformat(),
            'color': color_map.get(ev.break_type, '#7f8c8d'),
            'extendedProps': {
                'type': ev.break_type,
                'status': 'Academic',
                'course': '',
                'description': ''
            }
        })

    # Semester background
    academic_year = AcademicYear.query.first()
    if academic_year:
        events.append({
            'start': academic_year.semester_1_start.isoformat(),
            'end': (academic_year.semester_1_end + timedelta(days=1)).isoformat(),
            'display': 'background',
            'color': '#d1e7dd',
            'title': 'Semester 1'
        })
        events.append({
            'start': academic_year.semester_2_start.isoformat(),
            'end': (academic_year.semester_2_end + timedelta(days=1)).isoformat(),
            'display': 'background',
            'color': '#f8d7da',
            'title': 'Semester 2'
        })

    return render_template(
        'vclass/dashboard.html',
        programme=student_programme,
        level=student_level,
        quizzes=quiz_list,
        assignments=assignment_list,
        materials=material_list,
        events=events
    )

# Utility functions
def is_quiz_active(quiz):
    now = datetime.utcnow()
    return quiz.start_datetime <= now <= quiz.end_datetime

def is_quiz_submission_allowed(quiz):
    now = datetime.now()
    quiz_start = datetime.combine(quiz.date, quiz.start_time)
    quiz_end = quiz_start + timedelta(minutes=quiz.duration_minutes)
    # Allow submission only if current time <= quiz_end
    return now <= quiz_end

# Quiz instructions (single-shot)
@vclass_bp.route('/quiz-instructions/<int:quiz_id>')
@login_required
def quiz_instructions(quiz_id):
    quiz = Quiz.query.get_or_404(quiz_id)
    now = datetime.utcnow()

    if current_user.role != 'student':
        abort(403)

    if now < quiz.start_datetime:
        flash("This quiz is not yet available.", "warning")
        return redirect(url_for('vclass.dashboard'))

    if now > quiz.end_datetime:
        flash("This quiz has ended and can no longer be taken.", "danger")
        return redirect(url_for('vclass.dashboard'))

    # Check if already submitted (final, non-retakeable)
    existing_submission = StudentQuizSubmission.query.filter_by(
        quiz_id=quiz.id, student_id=current_user.id
    ).first()
    if existing_submission:
        flash("You have already submitted this quiz.", "info")
        return redirect(url_for('vclass.quiz_result', submission_id=existing_submission.id))

    # ✅ SINGLE-SHOT: No previous submission = can attempt
    attempts_made = 0
    can_attempt = True

    return render_template('vclass/quiz_instructions.html', quiz=quiz, attempts_made=attempts_made, can_attempt=can_attempt)

# Take quiz (single-shot)
@vclass_bp.route('/take-quiz/<int:quiz_id>')
@login_required
def take_quiz(quiz_id):
    if current_user.role != 'student':
        abort(403)

    quiz = Quiz.query.options(
        joinedload(Quiz.questions).joinedload(Question.options)
    ).get_or_404(quiz_id)

    now = datetime.utcnow()
    if now < quiz.start_datetime:
        flash("This quiz is not yet available.", "warning")
        return redirect(url_for('vclass.dashboard'))
    if now > quiz.end_datetime:
        flash("This quiz has ended.", "danger")
        return redirect(url_for('vclass.dashboard'))

    # If a final submission exists -> cannot take
    existing_submission = StudentQuizSubmission.query.filter_by(
        quiz_id=quiz.id, student_id=current_user.id
    ).first()
    if existing_submission:
        flash("You have already submitted this quiz and cannot retake it.", "warning")
        return redirect(url_for('vclass.quiz_result', submission_id=existing_submission.id))

    # Get or create a single QuizAttempt for this student+quiz (used for autosave)
    attempt = QuizAttempt.query.filter_by(
        quiz_id=quiz.id,
        student_id=current_user.id
    ).first()
    if not attempt:
        attempt = QuizAttempt(
            quiz_id=quiz.id,
            student_id=current_user.id,
            score=None,
            submitted_at=None
        )
        db.session.add(attempt)
        db.session.commit()  # ensure attempt.id available

    # Build quiz payload (don't expose sensitive internals)
    # ✅ IMPORTANT: Do NOT include correct_option_id - students must not see answers
    quiz_data = {
        "id": quiz.id,
        "title": quiz.title,
        "duration_minutes": quiz.duration_minutes,
        "max_score": quiz.max_score,
        "questions": [
            {
                "id": q.id,
                "question_text": q.text,
                "question_type": q.question_type,
                "marks": getattr(q, 'points', 1.0),
                "options": [
                    {"id": opt.id, "text": opt.text}
                    for opt in sorted((q.options or []), key=lambda x: x.id)
                ] if q.question_type in ("mcq", "multiple_choice", "MCQ") else []
            } for q in (quiz.questions or [])
        ]
    }

    # start timer in session if not already
    key = f'quiz_{quiz.id}_start_time'
    if key not in session:
        session[key] = datetime.utcnow().isoformat()
        session.modified = True

    # Load previously autosaved answers for this attempt
    saved_qs = (
        StudentAnswer.query
        .filter_by(attempt_id=attempt.id)
        .all()
    )
    saved_answers = {}
    for a in saved_qs:
        if a.selected_option_id is not None:
            saved_answers[a.question_id] = a.selected_option_id
        elif a.answer_text:
            try:
                saved_answers[a.question_id] = json.loads(a.answer_text)
            except Exception:
                saved_answers[a.question_id] = a.answer_text

    csrf_token_value = generate_csrf()

    return render_template(
        'vclass/take_quiz.html',
        quiz_json=quiz_data,
        questions=quiz.questions,
        attempt=attempt,
        session=session,
        csrf_token_value=csrf_token_value,
        saved_answers=saved_answers
    )


# Start quiz timer (AJAX) — sets session start time for a quiz
@vclass_bp.route('/start_quiz_timer/<int:quiz_id>', methods=['POST'])
@login_required
def start_quiz_timer(quiz_id):
    # Only students should start quiz timers
    if getattr(current_user, 'role', None) != 'student':
        abort(403)

    quiz = Quiz.query.get_or_404(quiz_id)
    now = datetime.utcnow()

    # Ensure quiz is currently available
    if now < quiz.start_datetime or now > quiz.end_datetime:
        return jsonify({'ok': False, 'error': 'quiz not available'}), 400

    key = f'quiz_{quiz.id}_start_time'
    session[key] = datetime.utcnow().isoformat()
    session.modified = True

    return jsonify({'ok': True})

# Autosave answer endpoint (single-shot)
@vclass_bp.route('/autosave_answer', methods=['POST'])
@login_required
def autosave_answer():
    """
    JSON: { quiz_id, question_id, selected_option_id?, answer_text? }
    Saves to StudentAnswer tied to the single QuizAttempt for this student+quiz.
    """
    if current_user.role != 'student':
        return jsonify({'ok': False, 'error': 'only students'}), 403

    data = request.get_json(silent=True) or {}
    quiz_id = data.get('quiz_id')
    question_id = data.get('question_id')
    selected_option_id = data.get('selected_option_id')
    answer_text = data.get('answer_text')

    if not quiz_id or not question_id:
        return jsonify({'ok': False, 'error': 'missing quiz_id or question_id'}), 400

    # If final submission exists -> disallow autosave
    if StudentQuizSubmission.query.filter_by(quiz_id=quiz_id, student_id=current_user.id).first():
        return jsonify({'ok': False, 'error': 'quiz already submitted'}), 400

    # Ensure attempt exists
    attempt = QuizAttempt.query.filter_by(quiz_id=quiz_id, student_id=current_user.id).first()
    if not attempt:
        attempt = QuizAttempt(quiz_id=quiz_id, student_id=current_user.id, score=None, submitted_at=None)
        db.session.add(attempt)
        db.session.flush()

    if isinstance(answer_text, (dict, list)):
        try:
            answer_text = json.dumps(answer_text)
        except Exception:
            answer_text = str(answer_text)

    ans = StudentAnswer.query.filter_by(attempt_id=attempt.id, question_id=question_id).first()
    if ans:
        ans.selected_option_id = selected_option_id
        ans.answer_text = answer_text
    else:
        ans = StudentAnswer(
            attempt_id=attempt.id,
            question_id=question_id,
            quiz_id=quiz_id,
            student_id=current_user.id,
            selected_option_id=selected_option_id,
            answer_text=answer_text
        )
        db.session.add(ans)

    try:
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

# Get saved answers (for restore). Returns empty if already submitted.
@vclass_bp.route('/get_saved_answers/<int:quiz_id>')
@login_required
def get_saved_answers(quiz_id):
    # If already submitted -> empty (no restore)
    if StudentQuizSubmission.query.filter_by(quiz_id=quiz_id, student_id=current_user.id).first():
        return jsonify({})

    attempt = QuizAttempt.query.filter_by(quiz_id=quiz_id, student_id=current_user.id).first()
    if not attempt:
        return jsonify({})

    answers = StudentAnswer.query.filter_by(attempt_id=attempt.id).all()
    result = {}
    for a in answers:
        if a.selected_option_id is not None:
            result[str(a.question_id)] = a.selected_option_id
        elif a.answer_text:
            try:
                result[str(a.question_id)] = json.loads(a.answer_text)
            except Exception:
                result[str(a.question_id)] = a.answer_text
        else:
            result[str(a.question_id)] = ""
    return jsonify(result)

# Submit quiz (single-shot grading + submission record)
@vclass_bp.route('/submit_quiz/<int:quiz_id>', methods=['POST'])
@login_required
def submit_quiz(quiz_id):
    if current_user.role != 'student':
        abort(403)

    quiz = Quiz.query.options(joinedload(Quiz.questions).joinedload(Question.options)).get_or_404(quiz_id)

    # Prevent double submission
    if StudentQuizSubmission.query.filter_by(quiz_id=quiz.id, student_id=current_user.id).first():
        flash("You have already submitted this quiz.", "warning")
        return redirect(url_for('vclass.dashboard'))

    # Attempt must exist (created on take_quiz/autosave)
    attempt = QuizAttempt.query.filter_by(quiz_id=quiz.id, student_id=current_user.id).first()
    if not attempt:
        # Defensive: create a new attempt if none (rare)
        attempt = QuizAttempt(quiz_id=quiz.id, student_id=current_user.id)
        db.session.add(attempt)
        db.session.flush()

    # Ensure `is_correct` column exists (might be missing after model change)
    try:
        inspector = inspect(db.engine)
        cols = [c['name'] for c in inspector.get_columns('student_answer')]
        if 'is_correct' not in cols:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE student_answer ADD COLUMN is_correct BOOLEAN DEFAULT 0"))
    except Exception as _:
        current_app.logger.debug('Could not ensure is_correct column exists: %s', _)

    # Load posted answers; fallback to autosaved DB answers
    saved_answers_db = { str(a.question_id): a for a in StudentAnswer.query.filter_by(attempt_id=attempt.id).all() }

    def get_posted_val(qid):
        # form may send multiple input formats: list or single
        vals = request.form.getlist(f'answers[{qid}][]')
        if vals and any(v != '' for v in vals):
            return vals
        val = request.form.get(f'answers[{qid}]')
        if val is not None and val != '':
            return val
        # fallback to saved DB
        saved = saved_answers_db.get(str(qid))
        if saved:
            return saved.selected_option_id if saved.selected_option_id is not None else (saved.answer_text or None)
        return None

    score = 0.0
    total_possible = 0.0

    # Build/update StudentAnswer rows and calculate score
    for q in quiz.questions:
        q_marks = float(getattr(q, 'points', 1.0) or 1.0)
        total_possible += q_marks

        submitted = get_posted_val(q.id)
        selected_option_id = None
        answer_text = None
        is_correct = False

        if q.question_type in ("mcq", "multiple_choice", "MCQ"):
            try:
                if submitted is not None and submitted != '':
                    selected_option_id = int(submitted)
            except (ValueError, TypeError):
                selected_option_id = None

            # Determine correct option id: prefer question.correct_option_id but
            # fall back to any option marked `is_correct` for backward compatibility.
            correct_opt_id = getattr(q, 'correct_option_id', None)
            if correct_opt_id is None:
                correct_opt = next((o for o in (q.options or []) if getattr(o, 'is_correct', False)), None)
                if correct_opt:
                    correct_opt_id = correct_opt.id

            if selected_option_id is not None and correct_opt_id is not None and int(selected_option_id) == int(correct_opt_id):
                score += q_marks
                is_correct = True

        else:
            # treat as short answer / manual: compare exact if possible
            if isinstance(submitted, list):
                answer_text = json.dumps(submitted)
            else:
                answer_text = (submitted or "").strip()

            # quick auto-grade if question has correct_answer or an option marked correct
            correct_answer = getattr(q, 'correct_answer', None) or None
            if not correct_answer:
                # try option marked correct
                correct_opt = next((o for o in (q.options or []) if getattr(o, 'is_correct', False)), None)
                if correct_opt:
                    correct_answer = correct_opt.text

            if correct_answer and answer_text:
                if answer_text.lower() == correct_answer.strip().lower():
                    score += q_marks
                    is_correct = True
                elif correct_answer.strip().lower() in answer_text.lower():
                    # partial credit
                    score += (q_marks * 0.5)
                    is_correct = True

        # persist StudentAnswer
        existing = saved_answers_db.get(str(q.id))
        if existing:
            existing.selected_option_id = selected_option_id
            existing.answer_text = answer_text
            existing.is_correct = is_correct
        else:
            new_ans = StudentAnswer(
                attempt_id=attempt.id,
                question_id=q.id,
                quiz_id=quiz.id,
                student_id=current_user.id,
                selected_option_id=selected_option_id,
                answer_text=answer_text,
                is_correct=is_correct
            )
            db.session.add(new_ans)

    # Finalize attempt and submission
    attempt.score = round(score, 2)
    attempt.submitted_at = datetime.utcnow()
    db.session.flush()

    submission = StudentQuizSubmission(
        student_id=current_user.id,
        quiz_id=quiz.id,
        score=round(score, 2),
        submitted_at=datetime.utcnow()
    )
    db.session.add(submission)
    db.session.commit()

    # clear timer session
    session.pop(f'quiz_{quiz.id}_start_time', None)

    flash(f"Quiz submitted! Your score: {round(score,2)}/{round(total_possible,2)}", "success")
    return redirect(url_for('vclass.quiz_result', submission_id=submission.id))

@vclass_bp.route('/has-submitted/<int:quiz_id>')
@login_required
def has_submitted(quiz_id):
    exists = StudentQuizSubmission.query.filter_by(
        student_id=current_user.id,
        quiz_id=quiz_id
    ).first()
    return jsonify({"submitted": bool(exists)})

@vclass_bp.route('/quiz_result/<int:submission_id>')
@login_required
def quiz_result(submission_id):
    submission = StudentQuizSubmission.query.get_or_404(submission_id)
    quiz = submission.quiz

    return render_template('vclass/quiz_result.html', quiz=quiz, submission=submission)

@vclass_bp.route('/grade-quiz-attempt/<int:attempt_id>', methods=['POST'])
@login_required
def grade_quiz_attempt(attempt_id):
    """Grade a completed quiz attempt."""
    attempt = QuizAttempt.query.get_or_404(attempt_id)
    
    # Verify permission
    if current_user.id != attempt.quiz.course.instructor_id and current_user.role != 'admin':
        abort(403)

    quiz = attempt.quiz
    total_score = 0
    max_score = 0

    # Grade each question
    for question in quiz.questions:
        max_score += question.marks or 0
        
        # Get student's answer for this question
        student_answer = StudentAnswer.query.filter_by(
            attempt_id=attempt_id,
            question_id=question.id
        ).first()

        if not student_answer:
            continue

        # Grade based on question type
        if question.question_type in ('mcq', 'multiple_choice', 'MCQ'):
            # ✅ CORRECT: Compare student's selected option with correct option
            if student_answer.selected_option_id == question.correct_option_id:
                total_score += question.marks or 0
                student_answer.is_correct = True
            else:
                student_answer.is_correct = False
        
        elif question.question_type in ('fill_blank', 'multi_blank', 'fill_in', 'fill-in'):
            # For fill-in-the-blank, you might need manual grading or keyword matching
            # This is more complex - implement as needed
            pass
        
        elif question.question_type in ('manual', 'short_answer', 'text'):
            # These require manual grading by teacher
            pass

        db.session.commit()

    # Store final score
    attempt.score = total_score
    attempt.max_score = max_score
    attempt.graded_at = datetime.utcnow()
    attempt.is_graded = True
    db.session.commit()

    # Create/update StudentQuizSubmission for final grade
    submission = StudentQuizSubmission.query.filter_by(
        student_id=attempt.student_id,
        quiz_id=attempt.quiz_id
    ).first()

    if not submission:
        submission = StudentQuizSubmission(
            student_id=attempt.student_id,
            quiz_id=attempt.quiz_id
        )
        db.session.add(submission)

    submission.score = total_score
    submission.max_score = max_score
    submission.is_graded = True
    submission.graded_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        "success": True,
        "score": total_score,
        "max_score": max_score,
        "percentage": (total_score / max_score * 100) if max_score > 0 else 0
    })

@vclass_bp.route('/download/assignments/<filename>')
@login_required
def download_assignment(filename):
    filepath = safe_join(current_app.config['UPLOAD_FOLDER'], filename)
    print(f"Looking for file: {filepath}")  # 🔍 DEBUG LINE
    if not os.path.exists(filepath):
        abort(404)
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

@vclass_bp.route('/assignment/<int:assignment_id>/submit', methods=['GET', 'POST'])
@login_required
def submit_assignment(assignment_id):
    assignment = Assignment.query.get_or_404(assignment_id)

    if request.method == 'POST':
        file = request.files.get('file')
        if not file or file.filename == '':
            flash("No file selected.", "danger")
            return redirect(request.url)

        if not allowed_file(file.filename):
            flash("Invalid file type. Allowed: doc, docx, xls, xlsx, pdf, ppt, txt", "danger")
            return redirect(request.url)

        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        submission = AssignmentSubmission(
            assignment_id=assignment.id,
            student_id=current_user.id,
            filename=filename,
            original_name=file.filename
        )
        db.session.add(submission)
        db.session.commit()

        flash("Assignment submitted successfully!", "success")
        return redirect(url_for('vclass.my_results'))

    return render_template("vclass/submit_assignment.html", assignment=assignment)

# ----------------------------------------------------------------
# OVERVIEW: show course cards with counts (cards page)
# ----------------------------------------------------------------
@vclass_bp.route('/materials')
@login_required
def materials_overview():
    """
    Show a card for each course that has materials (or 'General').
    """
    rows = CourseMaterial.query.order_by(CourseMaterial.upload_date.desc()).all()

    # group by course_name
    grouped = {}
    for m in rows:
        key = m.course_name or "General"
        grouped.setdefault(key, []).append(m)

    # build course card data (dicts)
    courses = []
    for course_name, items in grouped.items():
        # choose a thumbnail (first image if any)
        thumb = None
        for it in items:
            if (it.file_type or '').lower() in ('jpg','jpeg','png','gif','bmp','svg','webp'):
                thumb = url_for('vclass.preview_material', filename=it.filename)
                break
        # fallback: use a streaming/download link for first item
        if not thumb and items:
            thumb = url_for('vclass.download_material', filename=items[0].filename)

        courses.append({
            "course_name": course_name,
            "count": len(items),
            "thumbnail": thumb,
        })

    # sort by name
    courses = sorted(courses, key=lambda c: c['course_name'].lower())

    return render_template('vclass/materials_overview.html', courses=courses)


# ----------------------------------------------------------------
# COURSE DETAIL: show materials for a single course
# ----------------------------------------------------------------
@vclass_bp.route('/materials/course/<path:course_name>')
@login_required
def materials_by_course(course_name):
    """
    Show all materials for a course (course_name comes URL-encoded).
    """
    rows = CourseMaterial.query.filter_by(course_name=course_name).order_by(CourseMaterial.upload_date.desc()).all()
    if not rows:
        # Option: if no rows, show empty page / 404 / message
        return render_template('vclass/materials.html', course_name=course_name, materials=[])

    # convert models to json-serializable dicts (JS expects uploaded_at)
    material_list = []
    for m in rows:
        uploaded_iso = m.upload_date.isoformat() if m.upload_date else None
        material_list.append({
            "id": m.id,
            "title": m.title,
            "course_name": m.course_name,
            "programme_name": m.programme_name,      # <-- use this instead
            "programme_level": m.programme_level,    # <-- use this instead
            "filename": m.filename,
            "original_name": m.original_name,
            "file_type": m.file_type,
            "uploaded_at": uploaded_iso,
            "upload_date": uploaded_iso,
        })

    return render_template('vclass/materials.html', course_name=course_name, materials=material_list)


# ----------------------------------------------------------------
# PREVIEW + DOWNLOAD (keep your existing ones) - for reference
# ----------------------------------------------------------------
@vclass_bp.route('/preview/materials/<path:filename>')
@login_required
def preview_material(filename):
    materials_dir = current_app.config.get("MATERIALS_FOLDER") or os.path.join(os.getcwd(), "uploads", "materials")
    filepath = os.path.join(materials_dir, filename)
    if not os.path.exists(filepath):
        abort(404)

    mimetype, _ = mimetypes.guess_type(filename)
    response = send_from_directory(materials_dir, filename, as_attachment=False)
    if mimetype in ("application/pdf",) or (mimetype and mimetype.startswith("image/")):
        response.headers["Content-Disposition"] = f'inline; filename="{os.path.basename(filename)}"'
    return response


@vclass_bp.route('/download/materials/<path:filename>')
@login_required
def download_material(filename):
    materials_dir = current_app.config.get("MATERIALS_FOLDER") or os.path.join(os.getcwd(), "uploads", "materials")
    filepath = os.path.join(materials_dir, filename)
    if not os.path.exists(filepath):
        abort(404)
    return send_from_directory(materials_dir, filename, as_attachment=True)

@vclass_bp.route('/assignments')
@login_required
def assignments():
    assignments = Assignment.query.all()

    # Convert now to date
    now = datetime.utcnow().date()

    # Convert due_date to date for each assignment
    for a in assignments:
        a.due = a.due_date.date()  # new attribute for template comparisons

    return render_template(
        'vclass/assignments.html',
        assignments=assignments,
        now=now
    )

@vclass_bp.route('/material/video/<filename>')
@login_required
def play_video(filename):
    material = CourseMaterial.query.filter_by(filename=filename).first_or_404()

    if material.file_type.lower() not in ['mp4', 'webm', 'ogg']:
        flash('Unsupported video format.', 'warning')
        return redirect(url_for('vclass.virtual_class'))

    # Fetch related videos
    related_videos = CourseMaterial.query.filter(
        CourseMaterial.id != material.id,
        CourseMaterial.file_type.in_(['mp4', 'webm', 'ogg'])
    ).order_by(CourseMaterial.upload_date.desc()).limit(10).all()

    return render_template('vclass/play_video.html', material=material, related_videos=related_videos)

@vclass_bp.route('/stream/materials/<filename>')
@login_required
def stream_material_video(filename):
    video_path = os.path.join(current_app.root_path, 'uploads', 'materials', filename)
    if not os.path.isfile(video_path):
        abort(404)
    mime_type = f'video/{filename.rsplit(".", 1)[-1]}'

    # send_file with headers forcing inline content (stream)
    response = send_file(video_path, mimetype=mime_type, conditional=True)
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response

# Profile Page
@vclass_bp.route('/profile')
@login_required
def profile():
    if current_user.role != 'student':  # or 'teacher', depending
        abort(403)

    # Assuming you have a Profile model related to your User
    profile = getattr(current_user, 'profile', None)

    return render_template(
        'vclass/profile.html',
        user=current_user,  # pass current_user as 'user'
        profile=profile     # pass profile
    )

@vclass_bp.route('/student/meetings')
@login_required
def student_meetings():
    # 🔐 Role check
    if current_user.role != 'student':
        abort(403)

    # 👤 Validate student profile
    profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()
    if not profile:
        flash("Please complete your student profile first.", "warning")
        return redirect(url_for('student.dashboard'))

    # 📚 Get registered course IDs
    course_ids = [
        reg.course_id for reg in current_user.registered_courses
    ]

    # 🗓️ Load meetings (with Course preloaded)
    if course_ids:
        meetings = (
            Meeting.query
            .join(Course, Meeting.course_id == Course.id)
            .filter(Meeting.course_id.in_(course_ids))
            .order_by(Meeting.scheduled_start.desc())
            .all()
        )
    else:
        meetings = []

    now = datetime.utcnow()

    return render_template(
        'vclass/meetings.html',
        meetings=meetings,
        now=now
    )


@vclass_bp.route('/meeting/<int:meeting_id>')
@login_required
def join_meeting(meeting_id):
    """Render an Agora room only for its teacher or registered students."""
    meeting = Meeting.query.get_or_404(meeting_id)

    if current_user.role == 'teacher':
        if meeting.host_id != current_user.id:
            abort(403)
        role = 'host'
    elif current_user.role == 'student':
        registered_course_ids = {
            registration.course_id
            for registration in current_user.registered_courses
        }
        if meeting.course_id not in registered_course_ids:
            abort(403)
        role = 'audience'
    else:
        abort(403)

    try:
        whiteboard_sdk_token = current_app.config.get('WHITEBOARD_SDK_TOKEN')
        whiteboard_region = current_app.config.get('WHITEBOARD_REGION', 'us-sv')
        meeting_has_whiteboard_field = hasattr(meeting, 'whiteboard_uuid')
        whiteboard_uuid = getattr(meeting, 'whiteboard_uuid', None)
        whiteboard_token = None
        whiteboard_status = 'ready'

        if meeting_has_whiteboard_field and not whiteboard_uuid:
            meeting.whiteboard_uuid = create_whiteboard_room(
                whiteboard_sdk_token,
                whiteboard_region,
            )
            db.session.commit()
            whiteboard_uuid = meeting.whiteboard_uuid

        token = build_rtc_token(
            current_app.config.get('AGORA_APP_ID'),
            current_app.config.get('AGORA_APP_CERTIFICATE'),
            meeting.meeting_code,
            current_user.id,
            role,
            expires_in=3600,
        )
        if meeting_has_whiteboard_field and whiteboard_uuid:
            whiteboard_token = build_whiteboard_room_token(
                whiteboard_sdk_token,
                whiteboard_uuid,
                whiteboard_region,
                'admin' if role == 'host' else 'writer',
            )
        else:
            current_app.logger.warning(
                'Meeting model does not include whiteboard_uuid; starting Agora video without Whiteboard.'
            )
            whiteboard_status = 'migration_pending'
    except RuntimeError as exc:
        current_app.logger.error('Agora configuration error: %s', exc)
        flash(f'Live class service is unavailable: {exc}', 'danger')
        return redirect(
            url_for('teacher.meetings' if role == 'host' else 'vclass.student_meetings')
        )

    class_conv = ensure_meeting_class_conversation(meeting)

    return render_template(
        'vclass/agora_room.html',
        meeting=meeting,
        class_conversation_id=class_conv.id,
        class_conversation_name=class_conv.get_meta().get('name') or meeting.title,
        current_user_public_id=current_user.public_id,
        agora_app_id=current_app.config.get('AGORA_APP_ID'),
        agora_channel=meeting.meeting_code,
        agora_token=token,
        agora_uid=current_user.id,
        agora_role=role,
        whiteboard_app_identifier=current_app.config.get('WHITEBOARD_APP_IDENTIFIER'),
        whiteboard_region=current_app.config.get('WHITEBOARD_REGION', 'us-sv'),
        whiteboard_uuid=whiteboard_uuid,
        whiteboard_token=whiteboard_token,
        whiteboard_status=whiteboard_status,
        whiteboard_uid=str(current_user.id),
    )

@vclass_bp.route('/book-appointment', methods=['GET', 'POST'])
@login_required
def book_appointment():
    # Only fetch unbooked slots
    available_slots = AppointmentSlot.query.filter_by(is_booked=False).all()

    # Pass slots directly to template
    slots = []
    for slot in available_slots:
        teacher_user = slot.teacher.user  # Get the related User
        slots.append({
            'id': slot.id,
            'date': slot.date,
            'start_time': slot.start_time,
            'end_time': slot.end_time,
            'teacher_name': f"{teacher_user.first_name} {teacher_user.last_name}"
        })

    if request.method == 'POST':
        slot_id = request.form['slot_id']
        note = request.form.get('note', '')
        slot = AppointmentSlot.query.get_or_404(slot_id)

        if slot.is_booked:
            flash('Slot already booked.', 'danger')
            return redirect(url_for('vclass.book_appointment'))

        student_profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()
        if not student_profile:
            flash('Student profile not found.', 'danger')
            return redirect(url_for('vclass.book_appointment'))

        booking = AppointmentBooking(
            student_id=student_profile.id,
            slot_id=slot.id,
            note=note
        )
        slot.is_booked = True
        db.session.add(booking)
        db.session.add(booking)
        db.session.commit()

        flash('Appointment booked successfully.', 'success')
        return redirect(url_for('vclass.my_appointments'))

    return render_template('vclass/book_appointment.html', slots=slots)

@vclass_bp.route('/my_results')
@login_required
def my_results():
    user_id = current_user.id

    # -----------------------
    # QUIZZES (student submissions) - unchanged
    # -----------------------
    quiz_subs = StudentQuizSubmission.query.filter_by(student_id=user_id).all()
    quiz_results = []
    for sub in quiz_subs:
        quiz = getattr(sub, 'quiz', None)
        max_score = None
        if getattr(sub, 'max_score', None) is not None:
            max_score = float(sub.max_score)
        elif quiz is not None:
            if getattr(quiz, 'total_marks', None) is not None:
                max_score = float(quiz.total_marks)
            elif getattr(quiz, 'total_questions', None) is not None:
                points_each = float(getattr(quiz, 'points_per_question', 1) or 1)
                max_score = float(quiz.total_questions) * points_each

        obtained = float(sub.score) if getattr(sub, 'score', None) is not None else None
        percent = None
        if obtained is not None and max_score and max_score > 0:
            percent = (obtained / max_score) * 100.0

        quiz_results.append({
            'quiz': quiz,
            'score': obtained,
            'max_score': max_score,
            'percent': percent,
            'submitted_at': getattr(sub, 'submitted_at', None),
            'raw_submission': sub
        })

    # -----------------------
    # ASSIGNMENTS
    # -----------------------
    # 1) get student's submissions (map by assignment_id)
    assignment_subs = {}
    try:
        subs = AssignmentSubmission.query.filter_by(student_id=user_id).all()
        for s in subs:
            # ensure int key
            assignment_subs[int(getattr(s, 'assignment_id'))] = s
    except Exception as ex:
        # log if you want; keep map empty but don't break
        current_app.logger.debug("AssignmentSubmission fetch failed: %s", ex)
        assignment_subs = {}

    # 2) build list of assignments to show:
    #    - assignments for student's classes AND
    #    - any assignment ids that appear in student's submissions
    assignment_ids = set()
    try:
        student_classes = StudentCourseRegistration.query.filter_by(student_id=user_id).all()
        class_names = [sc.course.name for sc in student_classes if getattr(sc, 'course', None)]
        if class_names:
            assignments_by_class = Assignment.query.filter(Assignment.assigned_class.in_(class_names)).all()
            assignment_ids.update([a.id for a in assignments_by_class])
    except Exception as ex:
        current_app.logger.debug("Student class lookup failed: %s", ex)

    # include assignments that student submitted (guarantees they appear)
    assignment_ids.update(list(assignment_subs.keys()))

    # final fetch
    assignments = []
    if assignment_ids:
        assignments = Assignment.query.filter(Assignment.id.in_(list(assignment_ids))).all()

    # 3) build assignment_results (attach submission if exists)
    assignment_results = []
    for a in assignments:
        sub = assignment_subs.get(getattr(a, 'id'))

        # determine max_score from assignment fields (fallbacks if you use different fields)
        max_score = None
        if getattr(a, 'max_score', None) is not None:
            max_score = float(a.max_score)
        elif getattr(a, 'marks_allocated', None) is not None:
            max_score = float(a.marks_allocated)
        elif getattr(a, 'total_marks', None) is not None:
            max_score = float(a.total_marks)
        elif getattr(a, 'total_questions', None) is not None:
            points_each = float(getattr(a, 'points_per_question', 1) or 1)
            max_score = float(a.total_questions) * points_each

        obtained = None
        submitted_at = None
        grade_letter = None
        pass_fail = None
        if sub:
            obtained = float(getattr(sub, 'score')) if getattr(sub, 'score', None) is not None else None
            submitted_at = getattr(sub, 'submitted_at', None)
            grade_letter = getattr(sub, 'grade_letter', None)
            pass_fail = getattr(sub, 'pass_fail', None)

        percent = None
        if obtained is not None and max_score and max_score > 0:
            percent = (obtained / max_score) * 100.0

        assignment_results.append({
            'assignment': a,
            'score': obtained,
            'max_score': max_score,
            'percent': percent,
            'submitted_at': submitted_at,
            'submission': sub,
            'grade_letter': grade_letter,
            'pass_fail': pass_fail
        })

    # -----------------------
    # EXAMS (unchanged)
    # -----------------------
    exam_submissions = ExamSubmission.query.filter_by(student_id=user_id).all()
    exam_results = []
    for sub in exam_submissions:
        exam = getattr(sub, 'exam', None)
        max_score = None
        if getattr(sub, 'max_score', None) is not None:
            max_score = float(sub.max_score)
        elif getattr(sub, 'max_possible', None) is not None:
            max_score = float(sub.max_possible)
        elif exam is not None:
            if getattr(exam, 'total_marks', None) is not None:
                max_score = float(exam.total_marks)
            elif getattr(exam, 'total_questions', None) is not None:
                points_each = float(getattr(exam, 'points_per_question', 1) or 1)
                max_score = float(exam.total_questions) * points_each

        obtained = float(sub.score) if getattr(sub, 'score', None) is not None else None
        percent = None
        if obtained is not None and max_score and max_score > 0:
            percent = (obtained / max_score) * 100.0

        exam_results.append({
            'exam': exam,
            'score': obtained,
            'max_score': max_score,
            'percent': percent,
            'set_name': getattr(getattr(sub, 'exam_set', None), 'name', None),
            'submitted_at': getattr(sub, 'submitted_at', None),
            'raw_submission': sub
        })

    return render_template(
        "vclass/results.html",       # or the template file you actually use for student results
        quiz_results=quiz_results,
        assignment_results=assignment_results,
        exam_results=exam_results
    )

@vclass_bp.route('/calculator')
@login_required
def calculator():
    # vclass is for students only — still check role if you want:
    if getattr(current_user, "role", None) != "student":
        abort(403)
    return render_template('vclass/calculator.html')
