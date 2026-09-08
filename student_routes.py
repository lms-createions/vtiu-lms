import re

from flask import Blueprint, current_app, render_template, abort, redirect, url_for, flash, jsonify, session, send_from_directory, send_file, make_response

import json, os, secrets, requests

from flask import request

from flask_login import login_required, current_user, login_user

from sqlalchemy import func

from werkzeug.utils import safe_join, secure_filename

from models import ExamTimetableEntry, TeacherAssessment, TeacherAssessmentAnswer, TeacherAssessmentPeriod, TeacherAssessmentQuestion, TeacherCourseAssignment, TeacherProfile, db, User, Quiz, StudentQuizSubmission, Question, StudentProfile, Assignment, AssignmentSubmission, CourseMaterial, StudentCourseRegistration, Course,  TimetableEntry, AcademicCalendar, AcademicYear, AppointmentSlot, AppointmentBooking, StudentFeeBalance, ProgrammeFeeStructure, StudentFeeTransaction, Exam, ExamSubmission, ExamQuestion, ExamAttempt, ExamSet, ExamSetQuestion, Notification, NotificationRecipient, Meeting, StudentAnswer

from datetime import datetime

from forms import CourseRegistrationForm, ChangePasswordForm, StudentLoginForm

from io import BytesIO

from reportlab.lib.pagesizes import A4, landscape, letter

from reportlab.pdfgen import canvas

from reportlab.lib.units import inch

from reportlab.lib import colors

from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

from reportlab.lib.styles import getSampleStyleSheet

from reportlab.platypus import Image as RLImage

import io

from reportlab.lib.utils import ImageReader

import qrcode

from PIL import Image, ImageDraw

import textwrap

from utils.id_card import generate_student_id_card_pdf

from utils.result_builder import ResultBuilder

from utils.results_manager import ResultManager

from utils.result_templates import get_template_path
from utils.academic_year import configured_academic_year



student_bp = Blueprint('student', __name__, url_prefix='/student')



@student_bp.route('/login', methods=['GET', 'POST'])

def student_login():

    form = StudentLoginForm()



    if form.validate_on_submit():

        username = form.username.data.strip()

        user_id = form.user_id.data.strip()

        password = form.password.data.strip()



        user = User.query.filter_by(user_id=user_id, role='student').first()



        if user and user.username.lower() == username.lower() and user.check_password(password):

            login_user(user)

            flash(f"Welcome back, {user.first_name}!", "success")



            # 🔥 THIS IS THE MAGIC LINE

            next_page = request.args.get('next')

            if next_page and next_page.startswith('/'):

                return redirect(next_page)



            return redirect(url_for('student.dashboard'))



        flash("Invalid student credentials.", "danger")



    return render_template('student/login.html', form=form)



@student_bp.route('/dashboard')

@login_required

def dashboard():

    if current_user.role != 'student':

        abort(403)

    return render_template('student/dashboard.html', user=current_user)



@student_bp.route('/logout')

@login_required

def logout():

    """Logout student user"""

    logout_user()

    flash('You have been logged out successfully.', 'success')

    return redirect(url_for('student.login'))



@student_bp.app_context_processor

def inject_notification_count():

    unread_count = 0

    if current_user.is_authenticated:

        from models import NotificationRecipient



        if hasattr(current_user, "user_id"):  

            # Regular User (student, teacher)

            unread_count = NotificationRecipient.query.filter_by(

                user_id=current_user.user_id,

                is_read=False

            ).count()



        elif hasattr(current_user, "admin_id"):  

            # Admin → get all unread notifications

            unread_count = NotificationRecipient.query.filter_by(is_read=False).count()



    return dict(unread_count=unread_count)



from datetime import timedelta





@student_bp.route('/courses', methods=['GET', 'POST'])

@login_required

def register_courses():

    form = CourseRegistrationForm()

    student = current_user

    now = datetime.utcnow()

    start, registration_deadline = Course.get_registration_window()



    # 1️⃣ FETCH STUDENT PROFILE

    profile = StudentProfile.query.filter_by(user_id=student.user_id).first()

    if not profile:

        flash("Student profile not found.", "danger")

        return redirect(url_for("student.dashboard"))



    programme_name = profile.current_programme

    programme_level = str(profile.programme_level)  # Convert integer to string for database compatibility



    if not programme_name or not programme_level:

        flash("Programme information is incomplete. Contact admin.", "danger")

        return redirect(url_for("student.dashboard"))



    # 2️⃣ Use the administrator-configured academic year.
    configured_year = configured_academic_year()
    if not configured_year:

        flash("No academic years available yet. Contact admin.", "warning")

        return redirect(url_for("student.dashboard"))


    form.academic_year.choices = [(configured_year, configured_year)]



    # 3️⃣ STEP & SELECTIONS

    step = request.form.get("step")

    selected_sem = request.form.get("semester") or form.semester.data or 'First'

    selected_year = configured_year



    form.semester.data = selected_sem

    form.academic_year.data = selected_year



    # 4️⃣ FETCH COURSES

    courses = Course.query.filter_by(

        programme_name=programme_name,

        programme_level=programme_level,

        semester=selected_sem,

        academic_year=selected_year

    ).all()



    mandatory_courses = [c for c in courses if c.is_mandatory]

    optional_courses = [c for c in courses if not c.is_mandatory]



    form.courses.choices = [(c.id, f"{c.code} - {c.name}") for c in optional_courses]



    # 5️⃣ LOAD EXISTING REGISTRATIONS

    registered = StudentCourseRegistration.query.filter_by(

        student_id=student.id,

        semester=selected_sem,

        academic_year=selected_year

    ).all()

    form.courses.data = [r.course_id for r in registered if not r.course.is_mandatory]



    # 6️⃣ DEADLINE CHECK

    deadline_passed = registration_deadline and now > registration_deadline



    # 7️⃣ HANDLE SUBMISSION

    if request.method == "POST" and step == "register_courses" and form.validate_on_submit():

        if deadline_passed:

            flash("Registration deadline has passed.", "danger")

            return redirect(url_for("student.register_courses"))



        selected_ids = set(map(int, request.form.getlist('courses[]')))

        mandatory_ids = {c.id for c in mandatory_courses}

        final_course_ids = selected_ids | mandatory_ids



        # DELETE OLD RECORDS

        StudentCourseRegistration.query.filter_by(

            student_id=student.id,

            semester=selected_sem,

            academic_year=selected_year

        ).delete()

        db.session.commit()



        # SAVE NEW RECORDS

        for cid in final_course_ids:

            db.session.add(StudentCourseRegistration(

                student_id=student.id,

                course_id=cid,

                semester=selected_sem,

                academic_year=selected_year

            ))

        db.session.commit()



        flash("Courses registered successfully!", "success")

        return redirect(url_for("student.register_courses"))



    # 8️⃣ RENDER PAGE

    show_courses = (step == "select_semester") or len(registered) > 0



    return render_template(

        'student/courses.html',

        form=form,

        mandatory_courses=mandatory_courses,

        optional_courses=optional_courses,

        registered_courses=registered,

        show_courses=show_courses,

        registration_deadline=registration_deadline,

        deadline_passed=deadline_passed

    )





@student_bp.route('/courses/reset', methods=['POST'])

@login_required

def reset_registration():

    student = current_user



    semester = request.form.get("semester")

    year = request.form.get("academic_year")



    if not semester or not year:

        flash("Semester or Academic Year missing for reset.", "danger")

        return redirect(url_for("student.register_courses"))



    # Delete the current registration

    StudentCourseRegistration.query.filter_by(

        student_id=student.id,

        semester=semester,

        academic_year=year

    ).delete()

    db.session.commit()



    flash("Course registration has been reset. You may register again.", "info")

    return redirect(url_for("student.register_courses"))



@student_bp.route('/assessments')

@login_required

def view_assessments():

    """Show student's quiz, assignment, and exam results with raw scores and feedback"""

    student_user = current_user

    student_profile = StudentProfile.query.filter_by(user_id=student_user.user_id).first()

    

    if not student_profile:

        flash("Student profile not found", "danger")

        return redirect(url_for("student.dashboard"))



    # Get all courses student is registered for

    registrations = StudentCourseRegistration.query.filter_by(student_id=student_user.id).all()

    course_ids = [reg.course_id for reg in registrations]

    

    if not course_ids:

        return render_template("student/assessments.html", assessments=[], courses=[], message="No courses registered.")



    courses = Course.query.filter(Course.id.in_(course_ids)).all()

    course_dict = {c.id: c for c in courses}



    assessments = []



    # Quizzes

    quiz_subs = (

        db.session.query(StudentQuizSubmission, Quiz)

        .join(Quiz, Quiz.id == StudentQuizSubmission.quiz_id)

        .filter(StudentQuizSubmission.student_id == student_user.id)

        .filter(Quiz.course_id.in_(course_ids))

        .all()

    )

    

    for sub, quiz in quiz_subs:

        course = course_dict.get(quiz.course_id)

        assessments.append({

            "type": "Quiz",

            "course": course.name if course else "Unknown",

            "title": quiz.title,

            "raw_score": float(getattr(sub, "score", 0) or 0),

            "max_score": float(getattr(quiz, 'max_score', 0) or 0),

            "date": getattr(sub, "submitted_at", None),

            "feedback": None  # Can add feedback field to StudentQuizSubmission if needed

        })



    # Assignments

    assignment_subs = (

        db.session.query(AssignmentSubmission, Assignment)

        .join(Assignment, Assignment.id == AssignmentSubmission.assignment_id)

        .filter(AssignmentSubmission.student_id == student_user.id)

        .filter(Assignment.course_id.in_(course_ids))

        .filter(AssignmentSubmission.score != None)

        .all()

    )

    

    for sub, assignment in assignment_subs:

        course = course_dict.get(assignment.course_id)

        assessments.append({

            "type": "Assignment",

            "course": course.name if course else "Unknown",

            "title": assignment.title,

            "raw_score": float(getattr(sub, "score", 0) or 0),

            "max_score": float(getattr(assignment, 'max_score', 0) or 0),

            "date": getattr(sub, "submitted_at", None),

            "feedback": getattr(sub, "feedback", None)

        })



    # Exams

    exam_subs = (

        db.session.query(ExamSubmission, Exam)

        .join(Exam, Exam.id == ExamSubmission.exam_id)

        .filter(ExamSubmission.student_id == student_user.id)

        .filter(Exam.course_id.in_(course_ids))

        .filter(ExamSubmission.score != None)

        .all()

    )

    

    for sub, exam in exam_subs:

        course = course_dict.get(exam.course_id)

        assessments.append({

            "type": "Exam",

            "course": course.name if course else "Unknown",

            "title": exam.title,

            "raw_score": float(getattr(sub, "score", 0) or 0),

            "max_score": float(getattr(sub, 'max_score', 0) or 0),

            "date": getattr(sub, "submitted_at", None),

            "feedback": None

        })



    # Format dates

    for a in assessments:

        if a["date"]:

            try:

                a["date"] = a["date"].strftime("%Y-%m-%d %H:%M")

            except Exception:

                a["date"] = str(a["date"])

        else:

            a["date"] = ""



    # Sort by date (newest first)

    assessments.sort(key=lambda a: a.get("date") or "", reverse=True)



    return render_template(

        'student/assessments.html',

        assessments=assessments,

        courses=[c.name for c in courses],

        message=None

    )



@student_bp.route('/my_results')

@login_required

def my_results():

    data = ResultBuilder.semester(current_user.id)



    if not data["released"]:

        return render_template("student/results_not_released.html")



    return render_template(

        "student/results.html",

        results=data["results"],

        academic_year=data["academic_year"],

        semester=data["semester"]

    )







# View results as HTML

@student_bp.route("/results/view/<student_id>")

def view_results(student_id):

    from utils.result_render import ResultRenderer



    data = ResultBuilder.semester(student_id)

    return ResultRenderer.render_html(data)



# Download results as PDF

from utils.result_render import render_pdf as render_results_pdf



@student_bp.route("/results/pdf/<student_id>")

@login_required

def download_result(student_id):

    data = ResultBuilder.semester(student_id)

    if not data["released"]:

        abort(403, "Results not released yet")

    return render_results_pdf({

        "student_id": student_id,

        "results": data["results"],

        "academic_year": data["academic_year"],

        "semester": data["semester"]

    })



@student_bp.route("/student/results")

@login_required

def semester_results():

    data = ResultBuilder.semester(current_user.id)



    if not data["released"]:

        return render_template(

            "student/results_not_released.html"

        )



    return render_template(

        "student/results.html",

        results=data["results"],

        academic_year=data["academic_year"],

        semester=data["semester"]

    )



from services.result_builder import ResultBuilder



@student_bp.route("/student/transcript")

@login_required

def transcript():

    data = ResultBuilder.transcript(current_user.id)



    return render_template(

        "student/transcript.html",

        transcript=data["records"],

        overall_gpa=data["overall_gpa"]

    )



@student_bp.route('/courses/download-pdf', methods=['GET'])

@login_required

def download_registered_courses_pdf():

    """Download registered courses as PDF"""

    from flask import send_file

    from utils.course_registration_pdf import generate_course_registration_pdf

    

    try:

        # Get parameters from URL

        semester = request.args.get('semester')

        academic_year = request.args.get('academic_year')

        

        # Validate inputs

        if not semester or not academic_year:

            flash("Missing semester or academic year.", "danger")

            return redirect(url_for('student.register_courses'))

        

        student = current_user

        

        # Get registered courses from database

        registered = StudentCourseRegistration.query.filter_by(

            student_id=student.id,

            semester=semester,

            academic_year=academic_year

        ).all()

        

        # Check if there are any courses

        if not registered:

            flash("No courses registered for this semester.", "warning")

            return redirect(url_for('student.register_courses'))

        

        # Generate PDF with logo

        import os

        logo_path = os.path.join(os.path.dirname(__file__), 'static', 'VTIU-LOGO.png')

        

        pdf = generate_course_registration_pdf(

            student=student,

            registered_courses=registered,

            semester=semester,

            academic_year=academic_year,

            logo_path=logo_path

        )

        

        # Create filename

        filename = f"Registration_{student.user_id}_{semester}_{academic_year}.pdf"

        

        # Send file to user

        return send_file(

            pdf,

            mimetype='application/pdf',

            as_attachment=True,

            download_name=filename,

            conditional=False

        )

    

    except Exception as e:

        flash(f"Error: {str(e)}", "danger")

        return redirect(url_for('student.register_courses'))

        

from datetime import datetime

from flask import render_template, abort

from flask_login import login_required, current_user

from math import ceil

from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle



@student_bp.route('/timetable')

@login_required

def view_timetable():

    """View class timetable (tertiary version with programme/level)"""

    if current_user.role != 'student':

        abort(403)



    profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first_or_404()

    

    # For tertiary, filter by programme level instead of class

    # TimetableEntry.assigned_class now contains level like "100", "200", etc.

    entries = (

        TimetableEntry.query

        .filter_by(programme_level=str(profile.programme_level))

        .order_by(TimetableEntry.day_of_week, TimetableEntry.start_time)

        .all()

    )



    # === CUSTOM TIME SLOTS ===

    TIME_SLOTS = [

        (8*60, 9*60),

        (9*60, 10*60),

        (10*60, 10*60+30),

        (10*60+30, 11*60+30),

        (11*60+30, 12*60+30),

        (12*60+30, 13*60),

        (13*60, 14*60),

        (14*60, 15*60),

        (15*60, 16*60),

        (16*60, 17*60),

    ]



    # Build header ticks with widths (percent)

    MIN_START = TIME_SLOTS[0][0]

    MAX_END = TIME_SLOTS[-1][1]

    total_minutes = MAX_END - MIN_START



    time_ticks = []

    for start, end in TIME_SLOTS:

        width_pct = ((end - start) / total_minutes) * 100.0

        label = f"{(start//60) % 12 or 12}:{start%60:02d} - {(end//60) % 12 or 12}:{end%60:02d}"

        time_ticks.append({

            'start': start,

            'end': end,

            'label': label,

            'width_pct': round(width_pct, 4)

        })



    # Build vertical line positions

    cum = MIN_START

    vlines = []

    for start, end in TIME_SLOTS:

        cum += (end - start)

        cum_pct = ((cum - MIN_START) / total_minutes) * 100.0

        is_thick = ((end % 60) == 0)

        vlines.append({'left_pct': round(cum_pct, 3), 'is_thick': is_thick})



    # Day blocks

    day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']

    day_blocks = {d: [] for d in day_order}



    # Helper to compute left/width pct

    def pct_from_minutes(start_min, end_min):

        s = max(start_min, MIN_START)

        e = min(end_min, MAX_END)

        if e <= s:

            return None, None

        left_pct = ((s - MIN_START) / total_minutes) * 100.0

        width_pct = ((e - s) / total_minutes) * 100.0

        return round(left_pct, 3), round(width_pct, 3)



    # Add classes

    for e in entries:

        s_min = e.start_time.hour*60 + e.start_time.minute

        e_min = e.end_time.hour*60 + e.end_time.minute

        left_pct, width_pct = pct_from_minutes(s_min, e_min)

        if left_pct is None:

            continue

        day_blocks[e.day_of_week].append({

            'id': e.id,

            'title': e.course.name if getattr(e, 'course', None) else 'Class',

            'start_str': e.start_time.strftime('%I:%M %p'),

            'end_str': e.end_time.strftime('%I:%M %p'),

            'left_pct': left_pct,

            'width_pct': width_pct,

            'is_break': False

        })



    # ========= BREAKS =========

    MORNING_BREAK_LETTERS = ['B', 'R', 'E', 'A', 'K']

    AFTERNOON_BREAK_LETTERS = ['L', 'U', 'N', 'C', 'H']



    BREAKS = [

        {'title': 'Morning Break', 'start_min': 10*60, 'end_min': 10*60+25, 'letters': MORNING_BREAK_LETTERS},

        {'title': 'Lunch Break', 'start_min': 12*60+30, 'end_min': 12*60+55, 'letters': AFTERNOON_BREAK_LETTERS},

    ]



    for i, day in enumerate(day_order):

        for br in BREAKS:

            left_pct, width_pct = pct_from_minutes(br['start_min'], br['end_min'])

            if left_pct is None:

                continue

            day_blocks[day].append({

                'id': None,

                'title': br['letters'][i],

                'start_str': f"{br['start_min']//60:02d}:{br['start_min']%60:02d}",

                'end_str': f"{br['end_min']//60:02d}:{br['end_min']%60:02d}",

                'left_pct': left_pct,

                'width_pct': width_pct,

                'is_break': True

            })



    # Sort blocks per day

    for d in day_order:

        day_blocks[d].sort(key=lambda x: x['left_pct'])



    # CSS grid-template-columns value

    col_template = ' '.join(f'{slot["width_pct"]}%' for slot in time_ticks)



    return render_template(

        'student/timetable.html',

        programme=profile.current_programme,

        level=profile.programme_level,

        time_ticks=time_ticks,

        day_blocks=day_blocks,

        vlines=vlines,

        col_template=col_template,

        total_minutes=total_minutes,

        download_ts=int(datetime.utcnow().timestamp())

    )



@student_bp.route('/download_timetable')

@login_required

def download_timetable():

    """Download timetable as PDF (tertiary version)"""

    student_profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()

    if not student_profile:

        flash('Student profile not found.', 'danger')

        return redirect(url_for('student.view_timetable'))



    # Use programme level for filtering

    programme_level = str(student_profile.programme_level)

    programme_name = student_profile.current_programme



    timetable_entries = TimetableEntry.query \
        .filter_by(assigned_class=programme_level) \
        .join(Course, TimetableEntry.course_id == Course.id) \
        .order_by(TimetableEntry.day_of_week, TimetableEntry.start_time) \
        .all()



    if not timetable_entries:

        flash('No timetable available to download.', 'warning')

        return redirect(url_for('student.view_timetable'))



    # === TIME SLOTS ===

    TIME_SLOTS = [

        (8*60, 9*60),

        (9*60, 10*60),

        (10*60, 10*60+30),

        (10*60+30, 11*60+30),

        (11*60+30, 12*60+30),

        (12*60+30, 13*60),

        (13*60, 14*60),

        (14*60, 15*60),

        (15*60, 16*60),

        (16*60, 17*60),

    ]

    MIN_START = TIME_SLOTS[0][0]

    MAX_END = TIME_SLOTS[-1][1]

    total_minutes = MAX_END - MIN_START



    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']



    # Break letters

    MORNING_LETTERS = ['B', 'R', 'E', 'A', 'K']

    LUNCH_LETTERS = ['L', 'U', 'N', 'C', 'H']

    BREAKS = [

        {'start_min': 10*60, 'end_min': 10*60+25, 'letters': MORNING_LETTERS},

        {'start_min': 12*60+30, 'end_min': 12*60+55, 'letters': LUNCH_LETTERS},

    ]



    # Build header labels & column widths

    header = ['Day / Time']

    col_widths = [1.2 * inch]

    total_width = 10.5 * inch

    remaining_width = total_width - col_widths[0]



    for start, end in TIME_SLOTS:

        mins = end - start

        width = remaining_width * (mins / total_minutes)

        col_widths.append(width)

        header.append(f"{start//60:02d}:{start%60:02d} - {end//60:02d}:{end%60:02d}")



    # PDF Paragraph style

    styles = getSampleStyleSheet()

    cell_style = ParagraphStyle(

        'cell_style',

        parent=styles['Normal'],

        alignment=1,  # center

        fontSize=9,

        leading=10,

        wordWrap='CJK'

    )



    # Build timetable matrix

    timetable_matrix = []

    today_name = datetime.now().strftime('%A')



    for i, day in enumerate(days):

        row = [day]  # first column is day

        for start, end in TIME_SLOTS:

            match = next(

                (e for e in timetable_entries

                 if e.day_of_week == day and (e.start_time.hour*60 + e.start_time.minute) == start),

                None

            )

            if match:

                row.append(Paragraph(match.course.name, cell_style))

            else:

                # Check breaks

                letter = None

                for br in BREAKS:

                    if start >= br['start_min'] and start < br['end_min']:

                        letter = br['letters'][i]

                        break

                row.append(Paragraph(letter if letter else "—", cell_style))

        timetable_matrix.append(row)



    data = [header] + timetable_matrix



    # PDF Generation

    buffer = BytesIO()

    doc = SimpleDocTemplate(

        buffer,

        pagesize=landscape(A4),

        leftMargin=inch/2, rightMargin=inch/2,

        topMargin=inch/2, bottomMargin=inch/2

    )

    elements = []



    # TERTIARY TITLE FORMAT

    title_text = f"<b>{programme_name} - Level {programme_level} Timetable</b>"

    elements.append(Paragraph(title_text, styles['Title']))

    elements.append(Spacer(1, 12))



    table = Table(data, colWidths=col_widths, repeatRows=1)



    # Table style

    table_style = TableStyle([

        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#4A90E2")),

        ('TEXTCOLOR', (0,0), (-1,0), colors.white),

        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),

        ('ALIGN', (0,0), (-1,-1), 'CENTER'),

        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),

        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cccccc")),

    ])



    # Row colors + today highlight + break letters

    for i, row in enumerate(timetable_matrix):

        bg_color = colors.HexColor("#f0f4f8") if i % 2 == 0 else colors.white

        table_style.add('BACKGROUND', (0,i+1), (-1,i+1), bg_color)

        if row[0] == today_name:

            table_style.add('BACKGROUND', (0,i+1), (-1,i+1), colors.HexColor("#FFF4CC"))



        for j, val in enumerate(row[1:], start=1):

            text = val.getPlainText().strip()

            if text in MORNING_LETTERS + LUNCH_LETTERS:

                table_style.add('BACKGROUND', (j,i+1), (j,i+1), colors.HexColor("#FFD966"))

                table_style.add('TEXTCOLOR', (j,i+1), (j,i+1), colors.HexColor("#222222"))

                table_style.add('FONTNAME', (j,i+1), (j,i+1), 'Helvetica-Bold')



    table.setStyle(table_style)

    elements.append(table)



    elements.append(Spacer(1, 12))

    elements.append(Paragraph(f"Generated on: {datetime.now().strftime('%d %b %Y %I:%M %p')}", styles['Normal']))



    doc.build(elements)

    buffer.seek(0)



    # TERTIARY FILENAME FORMAT

    filename = f"{programme_name}_Level{programme_level}_timetable.pdf"

    return send_file(

        buffer,

        as_attachment=True,

        download_name=filename,

        mimetype='application/pdf'

    )



# Appointment Booking System

from collections import defaultdict



@student_bp.route('/book-appointment', methods=['GET', 'POST'])

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

            return redirect(url_for('student.book_appointment'))



        student_profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()

        if not student_profile:

            flash('Student profile not found.', 'danger')

            return redirect(url_for('student.book_appointment'))



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

        return redirect(url_for('student.my_appointments'))



    return render_template('student/book_appointment.html', slots=slots)



from sqlalchemy.orm import joinedload



@student_bp.route('/my-appointments')

@login_required

def my_appointments():

    student_profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()

    if not student_profile:

        flash('Student profile not found.', 'danger')

        return redirect(url_for('student.book_appointment'))

    bookings = AppointmentBooking.query \
        .filter_by(student_id=student_profile.id) \
        .options(joinedload(AppointmentBooking.slot).joinedload(AppointmentSlot.teacher)) \
        .all()

    return render_template('student/my_appointments.html', bookings=bookings)



# Fees Management

@student_bp.route('/fees')

@login_required

def student_fees():

    # Restrict to students

    if current_user.role != 'student':

        abort(403)



    fees = StudentFeeBalance.query.filter_by(

        student_id=current_user.id

    ).order_by(StudentFeeBalance.id.desc()).all()



    transactions = StudentFeeTransaction.query.filter_by(

        student_id=current_user.id

    ).order_by(StudentFeeTransaction.timestamp.desc()).all()



    return render_template(

        'student/fees.html',

        fees=fees,

        transactions=transactions

    )


@student_bp.route('/pay-fees', methods=['GET', 'POST'])
@login_required
def pay_fees():
    if current_user.role != 'student':
        abort(403)

    student = current_user
    profile = StudentProfile.query.filter_by(user_id=student.user_id).first()

    if not profile:
        flash("Student profile not found.", "danger")
        return redirect(url_for('main.index'))

    programme = profile.current_programme
    level = str(int(profile.programme_level)) if profile.programme_level else '100'
    study_format = profile.study_format or 'Regular'

    year = configured_academic_year()
    if not year:
        flash("The academic year has not been configured by an administrator.", "warning")
        return redirect(url_for('main.index'))
    semester = request.args.get('semester') or 'First'

    # Get fees
    fee_structures = ProgrammeFeeStructure.query.filter_by(
        programme_name=programme,
        programme_level=level,
        study_format=study_format,
        academic_year=year,
        semester=semester
    ).all()

    total_fee = sum(f.amount for f in fee_structures) if fee_structures else 0.0
    
    # Get fee percentage settings
    from models import FeePercentageSettings
    fee_settings = FeePercentageSettings.get_active_settings(year)
    
    # Calculate base payment requirement
    if fee_settings:
        base_payment_required = (fee_settings.base_payment_percentage / 100.0) * total_fee
        base_payment_deadline = fee_settings.base_payment_deadline
        allow_installments = fee_settings.allow_installments_after_base
    else:
        base_payment_required = total_fee  # Default to full payment if no settings
        base_payment_deadline = None
        allow_installments = True

    # Get approved payments
    approved_txns = StudentFeeTransaction.query.filter_by(
        student_id=student.id,
        academic_year=year,
        semester=semester,
        is_approved=True
    ).all()
    current_balance = sum(txn.amount for txn in approved_txns)
    remaining = max(0, total_fee - current_balance)
    
    # Check if base payment has been made
    base_payment_made = current_balance >= base_payment_required
    
    # POST: Submit payment
    if request.method == 'POST':
        if request.form.get('method') == 'paystack':
            flash("Please use the Paystack checkout button to complete this payment.", "warning")
            return redirect(url_for('student.pay_fees', year=year, semester=semester))
        amount = float(request.form.get('amount', 0))
        
        # VALIDATION: Base payment requirement
        if not base_payment_made and amount < base_payment_required:
            flash(f"Base payment of GHS {base_payment_required:.2f} is required before installments. Current payment: GHS {amount:.2f}", "danger")
            return redirect(url_for('student.pay_fees', year=year, semester=semester))
        
        # VALIDATION: Cannot pay more than remaining
        if amount > remaining:
            flash(f"Cannot pay more than GHS {remaining:.2f}", "danger")
            return redirect(url_for('student.pay_fees', year=year, semester=semester))

        if amount <= 0:
            flash("Amount must be greater than 0", "danger")
            return redirect(url_for('student.pay_fees', year=year, semester=semester))

        description = request.form.get('description') or "School Fees"

        txn = StudentFeeTransaction(
            student_id=student.id,
            academic_year=year,
            semester=semester,
            amount=amount,
            description=description,
            payment_method=request.form.get('method') or 'manual',
            is_approved=False,
            timestamp=datetime.utcnow()
        )
        db.session.add(txn)
        db.session.commit()

        flash(f"✓ Payment of GHS {amount:.2f} submitted", "success")
        return redirect(url_for('student.pay_fees', year=year, semester=semester))

    # Available years
    years = db.session.query(ProgrammeFeeStructure.academic_year).filter_by(
        programme_name=programme,
        programme_level=level,
        study_format=study_format
    ).distinct().order_by(ProgrammeFeeStructure.academic_year.desc()).all()
    available_years = [y[0] for y in years]

    # Determine if installments are allowed based on level
    # Level 100 (freshers): Full payment only
    # Level 200+: Installments allowed
    allow_installments = int(level) >= 200

    return render_template(
        'student/pay_fees.html',
        assigned_fees=fee_structures,
        total_fee=total_fee,
        current_balance=current_balance,
        remaining=remaining,
        max_allowed_amount=remaining,
        year=year,
        semester=semester,
        available_years=available_years,
        transactions=approved_txns,
        programme=programme,
        level=level,
        allow_installments=allow_installments,
        student_level=int(level),
        base_payment_required=base_payment_required if fee_settings else total_fee,
        base_payment_deadline=base_payment_deadline,
        fee_settings=fee_settings,
        paystack_mode=(fee_structures[0].paystack_mode if fee_structures else 'test')
    )


@student_bp.route('/paystack/initialize', methods=['POST'])
@login_required
def paystack_initialize():
    """Create a Paystack checkout for the selected school-fee amount."""
    if current_user.role != 'student':
        abort(403)

    payload = request.get_json(silent=True) or request.form
    year = configured_academic_year()
    if not year:
        return jsonify({'success': False, 'message': 'Academic year is not configured.'}), 503
    semester = payload.get('semester') or 'First'
    profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()
    if not profile:
        return jsonify({'success': False, 'message': 'Student profile not found.'}), 400

    level = str(int(profile.programme_level)) if profile.programme_level else '100'
    fee_structures = ProgrammeFeeStructure.query.filter_by(
        programme_name=profile.current_programme,
        programme_level=level,
        study_format=profile.study_format or 'Regular',
        academic_year=year,
        semester=semester
    ).all()
    total_fee = sum(f.amount for f in fee_structures) if fee_structures else 0.0
    approved_total = db.session.query(db.func.coalesce(db.func.sum(StudentFeeTransaction.amount), 0)).filter_by(
        student_id=current_user.id, academic_year=year, semester=semester, is_approved=True
    ).scalar() or 0
    remaining = max(0, float(total_fee) - float(approved_total))

    try:
        amount = float(payload.get('amount', 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid payment amount.'}), 400
    if amount <= 0 or amount > remaining:
        return jsonify({'success': False, 'message': f'Amount must be between 0 and GHS {remaining:.2f}.'}), 400
    if not fee_structures:
        return jsonify({'success': False, 'message': 'No fee structure found for this period.'}), 400

    mode = fee_structures[0].paystack_mode if fee_structures[0].paystack_mode in {'test', 'live'} else 'test'
    secret_key = current_app.config.get(f'PAYSTACK_{mode.upper()}_SECRET_KEY')
    if not secret_key:
        return jsonify({'success': False, 'message': f'Paystack {mode} mode is not configured.'}), 503

    reference = f"fees-{current_user.user_id}-{secrets.token_urlsafe(16)}"
    transaction = StudentFeeTransaction(
        student_id=current_user.id,
        academic_year=year,
        semester=semester,
        amount=round(amount, 2),
        description=payload.get('description') or 'School Fees - Paystack',
        payment_method='paystack',
        paystack_mode=mode,
        paystack_reference=reference,
        is_approved=False,
        timestamp=datetime.utcnow()
    )
    db.session.add(transaction)

    callback_url = (
        current_app.config.get(f'PAYSTACK_{mode.upper()}_CALLBACK_URL')
        or current_app.config.get('PAYSTACK_CALLBACK_URL')
        or url_for(
        'student.paystack_verify', reference=reference, _external=True
        )
    )
    try:
        response = requests.post(
            'https://api.paystack.co/transaction/initialize',
            headers={'Authorization': f'Bearer {secret_key}', 'Content-Type': 'application/json'},
            json={
                'email': current_user.email,
                'amount': int(round(amount * 100)),
                'currency': current_app.config.get('PAYSTACK_CURRENCY', 'GHS'),
                'reference': reference,
                'callback_url': callback_url,
                'metadata': {'student_id': current_user.user_id, 'academic_year': year, 'semester': semester}
            },
            timeout=20
        )
        result = response.json()
        if not response.ok or not result.get('status') or not result.get('data', {}).get('authorization_url'):
            db.session.rollback()
            return jsonify({'success': False, 'message': result.get('message', 'Paystack initialization failed.')}), 502
        db.session.commit()
        return jsonify({'success': True, 'authorization_url': result['data']['authorization_url'], 'reference': reference})
    except (requests.RequestException, ValueError) as exc:
        db.session.rollback()
        current_app.logger.exception('Paystack initialization failed: %s', exc)
        return jsonify({'success': False, 'message': 'Unable to connect to Paystack.'}), 502


@student_bp.route('/paystack/verify/<string:reference>')
@login_required
def paystack_verify(reference):
    """Verify a Paystack reference and approve the matching local transaction."""
    if current_user.role != 'student':
        abort(403)
    transaction = StudentFeeTransaction.query.filter_by(
        paystack_reference=reference, student_id=current_user.id, payment_method='paystack'
    ).first_or_404()
    if transaction.is_approved:
        flash('This Paystack payment is already confirmed.', 'info')
        return redirect(url_for('student.pay_fees', year=transaction.academic_year, semester=transaction.semester))

    mode = transaction.paystack_mode if transaction.paystack_mode in {'test', 'live'} else 'test'
    secret_key = current_app.config.get(f'PAYSTACK_{mode.upper()}_SECRET_KEY')
    if not secret_key:
        flash(f'Paystack {mode} mode is not configured.', 'danger')
        return redirect(url_for('student.pay_fees', year=transaction.academic_year, semester=transaction.semester))

    try:
        response = requests.get(
            f'https://api.paystack.co/transaction/verify/{reference}',
            headers={'Authorization': f'Bearer {secret_key}'}, timeout=20
        )
        result = response.json()
        data = result.get('data') or {}
        expected_amount = int(round(transaction.amount * 100))
        currency = current_app.config.get('PAYSTACK_CURRENCY', 'GHS')
        valid = (
            response.ok and result.get('status') and data.get('status') == 'success'
            and data.get('reference') == reference
            and int(data.get('amount', 0)) == expected_amount
            and data.get('currency') == currency
        )
        if not valid:
            flash('Paystack could not verify this payment.', 'danger')
        else:
            transaction.is_approved = True
            transaction.timestamp = datetime.utcnow()
            db.session.commit()
            flash(f'Payment of GHS {transaction.amount:.2f} confirmed successfully.', 'success')
    except (requests.RequestException, ValueError, TypeError) as exc:
        db.session.rollback()
        current_app.logger.exception('Paystack verification failed: %s', exc)
        flash('Unable to verify the Paystack payment right now.', 'danger')
    return redirect(url_for('student.pay_fees', year=transaction.academic_year, semester=transaction.semester))


@student_bp.route('/paystack/callback')
@login_required
def paystack_callback():
    """Accept a fixed Paystack callback URL and verify its reference."""
    reference = request.args.get('reference', '').strip()
    if not reference:
        flash('Paystack did not return a payment reference.', 'danger')
        return redirect(url_for('student.pay_fees'))
    return paystack_verify(reference)


@student_bp.route('/download-receipt/<int:txn_id>')

@login_required

def download_receipt(txn_id):

    txn = StudentFeeTransaction.query.get_or_404(txn_id)



    # Allow only the student

    if current_user.id != txn.student_id:

        abort(403)



    if not txn.is_approved:

        abort(403)



    filename = f"receipt_{txn.id}.pdf"

    filepath = os.path.join(current_app.config['RECEIPT_FOLDER'], filename)



    if not os.path.exists(filepath):

        flash("Receipt not found. Please contact admin.", "danger")

        return redirect(url_for('student.student_fees'))



    return send_file(filepath, as_attachment=True)





@student_bp.route('/profile')

@login_required

def profile():

    if not current_user.is_student:

        abort(403)



    profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()

    return render_template('student/profile.html', profile=profile, user=current_user)



@student_bp.route('/id-card')

@login_required

def view_id_card():

    if not current_user.is_student:

        abort(403)

    

    id_card_url = generate_student_id_card_pdf(current_user)

    return render_template(

        'student/view_id_card.html',

        id_card_url=id_card_url,

        student=current_user  # <-- pass the student here

    )



@student_bp.route('/profile/edit', methods=['GET', 'POST'])

@login_required

def edit_profile():

    if not current_user.is_student:

        abort(403)



    profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first_or_404()



    if request.method == 'POST':

        profile.phone = request.form.get('phone')

        profile.email = request.form.get('email')

        profile.address = request.form.get('address')

        profile.city = request.form.get('city')

        profile.postal_code = request.form.get('postal_code')



        profile.blood_group = request.form.get('blood_group')

        profile.medical_conditions = request.form.get('medical_conditions')



        profile.emergency_contact_name = request.form.get('emergency_contact_name')

        profile.emergency_contact_number = request.form.get('emergency_contact_number')



        db.session.commit()

        flash('Profile updated successfully.', 'success')

        return redirect(url_for('student.profile'))



    return render_template('student/edit_profile.html', profile=profile)



@student_bp.route('/change_password', methods=['GET', 'POST'])

@login_required

def change_password():

    form = ChangePasswordForm()

    if form.validate_on_submit():

        if current_user.check_password(form.current_password.data):

            current_user.set_password(form.new_password.data)

            db.session.commit()

            flash('Password updated successfully!', 'success')

            return redirect(url_for('student.profile'))

        else:

            flash('Current password is incorrect.', 'danger')

    return render_template('student/change_password.html', form=form)



from collections import defaultdict



@student_bp.route('/notifications')

@login_required

def student_notifications():

    """

    Show grouped notifications by title/category.

    """

    recipients = (

        NotificationRecipient.query

        .join(Notification, Notification.id == NotificationRecipient.notification_id)

        .filter(NotificationRecipient.user_id == current_user.user_id)

        .order_by(Notification.created_at.desc())

        .all()

    )



    grouped = defaultdict(list)

    for r in recipients:

        grouped[r.notification.title].append(r)



    # sort groups by most recent notification

    grouped_notifications = sorted(

        grouped.items(),

        key=lambda g: g[1][0].notification.created_at,

        reverse=True

    )



    return render_template(

        'student/notifications.html',

        grouped_notifications=grouped_notifications

    )



@student_bp.route('/notifications/group/<string:title>')

@login_required

def view_notification_group(title):

    """

    Show all notifications under a single title group.

    """

    recipients = (

        NotificationRecipient.query

        .join(Notification, Notification.id == NotificationRecipient.notification_id)

        .filter(NotificationRecipient.user_id == current_user.user_id,

                Notification.title == title)

        .order_by(Notification.created_at.desc())

        .all()

    )



    # mark all as read

    for r in recipients:

        if not r.is_read:

            r.is_read = True

            r.read_at = datetime.utcnow()

    db.session.commit()



    return render_template('student/notification_detail.html', title=title, recipients=recipients)





@student_bp.route('/notifications/mark_read/<int:recipient_id>', methods=['POST'])

@login_required

def mark_notification_read(recipient_id):

    recipient = NotificationRecipient.query.filter_by(

        id=recipient_id, user_id=current_user.user_id

    ).first_or_404()



    if not recipient.is_read:

        recipient.is_read = True

        recipient.read_at = datetime.utcnow()

        db.session.commit()



    return jsonify({"success": True, "id": recipient_id})



@student_bp.route('/notifications/delete/<int:recipient_id>', methods=['POST'])

@login_required

def delete_notification(recipient_id):

    """

    Delete a single notification for the logged-in student.

    """

    recipient = NotificationRecipient.query.filter_by(

        id=recipient_id, user_id=current_user.user_id

    ).first_or_404()



    db.session.delete(recipient)

    db.session.commit()



    return jsonify({"success": True, "id": recipient_id})



@student_bp.route('/notifications/delete_group/<string:title>', methods=['POST'])

@login_required

def delete_notification_group(title):

    """

    Delete all notifications under a given title for the current user.

    """

    recipients = (

        NotificationRecipient.query

        .join(Notification, Notification.id == NotificationRecipient.notification_id)

        .filter(

            NotificationRecipient.user_id == current_user.user_id,

            Notification.title == title

        )

        .all()

    )



    if not recipients:

        return jsonify({'success': False, 'message': 'No notifications found'}), 404



    # Delete each recipient entry

    for r in recipients:

        db.session.delete(r)

    db.session.commit()



    return jsonify({'success': True})





def format_time(t):

    # expects time object

    return t.strftime('%I:%M%p').lstrip('0').replace('AM','AM').replace('PM','PM')



@student_bp.route('/exam-timetable', methods=['GET', 'POST'])

@login_required

def exam_timetable_page():

    return render_template('student/exam_timetable_input.html')



def generate_logo_qr(data: str,

                     logo_path: str = None,

                     final_size: int = 300,

                     logo_fraction: float = 0.45,

                     box_size: int = 10,

                     border: int = 2) -> Image.Image:

    """Generate QR code with optional logo overlay."""

    qr = qrcode.QRCode(

        version=None,

        error_correction=qrcode.constants.ERROR_CORRECT_H,

        box_size=box_size,

        border=border

    )

    qr.add_data(data)

    qr.make(fit=True)

    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    

    # Resize to final size if needed

    if final_size and qr_img.size[0] != final_size:

        qr_img = qr_img.resize((final_size, final_size), Image.Resampling.LANCZOS)

    

    # Add logo if provided

    if logo_path:

        try:

            logo = Image.open(logo_path).convert("RGBA")

            

            # Calculate logo size based on fraction

            logo_size = int(qr_img.size[0] * logo_fraction)

            logo = logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS)

            

            # Create white background for logo

            logo_bg = Image.new("RGB", (logo_size + 10, logo_size + 10), "white")

            logo_bg.paste(logo, (5, 5), logo if logo.mode == "RGBA" else None)

            

            # Paste logo in center

            logo_pos = (qr_img.size[0] // 2 - logo_bg.size[0] // 2,

                       qr_img.size[1] // 2 - logo_bg.size[1] // 2)

            qr_img.paste(logo_bg, logo_pos)

        except Exception as e:

            # If logo loading fails, just return QR code without logo

            pass

    

    return qr_img





@student_bp.route('/exam-timetable/download', methods=['POST'])

@login_required

def download_student_exam_timetable():

    """Download exam timetable for tertiary student (by index number)"""

    index_number = request.form.get("index_number")

    if not index_number:

        flash("Please enter a valid index number.", "danger")

        return redirect(url_for('student.exam_timetable_page'))



    # Find student by index number (tertiary identifier)

    profile = StudentProfile.query.filter_by(index_number=index_number).first()

    if not profile:

        flash("Index number not found.", "danger")

        return redirect(url_for('student.exam_timetable_page'))



    user = profile.user

    if not user:

        flash("Student profile incomplete.", "danger")

        return redirect(url_for('student.exam_timetable_page'))



    # Get exam timetable entries for this programme level

    entries = ExamTimetableEntry.query.filter(

        ((ExamTimetableEntry.student_index == index_number) |

         (ExamTimetableEntry.programme_level == str(profile.programme_level)))

    ).order_by(

        ExamTimetableEntry.date,

        ExamTimetableEntry.start_time

    ).all()



    if not entries:

        flash("No exam timetable found for this index number.", "warning")

        return redirect(url_for('student.exam_timetable_page'))



    # Layout constants

    margin = 40

    block_spacing = 22

    block_corner_radius = 8

    page_width, page_height = letter

    content_width = page_width - 2 * margin



    qr_display_size = 120

    qr_generate_size = qr_display_size * 2

    qr_right_margin = 20



    buffer = BytesIO()

    p = canvas.Canvas(buffer, pagesize=letter)

    width, height = letter



    # -------- Header --------

    p.setFillColor(colors.HexColor("#1f77b4"))

    p.rect(0, height-80, width, 80, fill=True, stroke=False)

    p.setFillColor(colors.white)

    p.setFont("Helvetica-Bold", 18)

    p.drawCentredString(width/2, height-48, "END OF SEMESTER EXAMINATION TIMETABLE")

    p.setFont("Helvetica", 11)

    p.drawCentredString(width/2, height-68, f"{profile.academic_year or 'Academic Year'}")



    y_top = height - 100



    for e in entries:

        # 3-COLUMN LAYOUT

        col1_fields = [

            ("Name:", f"{user.first_name} {user.last_name}"),

            ("Index #:", index_number),

            ("Programme:", profile.current_programme or ""),

            ("Course:", e.course or ""),

            ("Level:", f"Level {profile.programme_level}")

        ]

        

        col2_fields = [

            ("Time:", f"{e.start_time.strftime('%H:%M')} - {e.end_time.strftime('%H:%M')}"),

            ("Date:", e.date.strftime('%A, %d %B %Y')),

            ("Room:", e.room or ""),

            ("Building:", e.building or ""),

            ("Floor:", e.floor or "")

        ]



        # Wrap long course name

        wrapped_course = textwrap.wrap(col1_fields[3][1], width=20)

        col1_fields[3] = ("Course:", "\n".join(wrapped_course))

        

        # Wrap long date

        wrapped_date = textwrap.wrap(col2_fields[1][1], width=20)

        col2_fields[1] = ("Date:", "\n".join(wrapped_date))



        # Layout constants - INCREASED SPACING

        line_height = 16

        top_padding = 20

        bottom_padding = 16

        left_margin_col = 14

        value_offset = 65

        col_width = (content_width - 4 * left_margin_col - qr_display_size) / 2



        # Calculate block height

        col1_lines = sum(v.count("\n") + 1 for _, v in col1_fields)

        col2_lines = sum(v.count("\n") + 1 for _, v in col2_fields)

        text_lines = max(col1_lines, col2_lines)

        text_block_height = text_lines * line_height + top_padding + bottom_padding

        block_height = max(text_block_height, qr_display_size + top_padding + bottom_padding)

        block_bottom = y_top - block_height



        # New page check - MORE SPACE AT BOTTOM

        if block_bottom < 60:

            p.showPage()

            p.setFillColor(colors.HexColor("#1f77b4"))

            p.rect(0, height-80, width, 80, fill=True, stroke=False)

            p.setFillColor(colors.white)

            p.setFont("Helvetica-Bold", 18)

            p.drawCentredString(width/2, height-48, "END OF SEMESTER EXAMINATION TIMETABLE")

            p.setFont("Helvetica", 11)

            p.drawCentredString(width/2, height-68, f"{profile.academic_year or 'Academic Year'}")

            y_top = height - 100

            block_bottom = y_top - block_height



        # Draw block background with border

        p.setFillColor(colors.HexColor("#f8f9fa"))

        p.roundRect(margin, block_bottom, content_width, block_height, block_corner_radius, fill=True, stroke=False)

        

        # Left accent bar - THICKER

        p.setFillColor(colors.HexColor("#1f77b4"))

        p.roundRect(margin+10, block_bottom+10, 8, block_height-20, 4, fill=True, stroke=False)

        

        # Border around box

        p.setStrokeColor(colors.HexColor("#d0d0d0"))

        p.setLineWidth(1)

        p.roundRect(margin, block_bottom, content_width, block_height, block_corner_radius, fill=False, stroke=True)



        # VERTICALLY CENTERED START Y POSITION (aligned with QR code middle)

        qr_x = margin + content_width - left_margin_col - qr_display_size - 8

        qr_y = block_bottom + (block_height - qr_display_size) / 2

        qr_center_y = qr_y + qr_display_size / 2

        

        # Start text from center of QR code, going upward

        centered_start_y = qr_center_y + (text_lines * line_height / 2)



        # Column 1 (Left) - VERTICALLY ALIGNED WITH QR CODE

        col1_x = margin + left_margin_col + 8

        cur_y = centered_start_y

        p.setFont("Helvetica-Bold", 11)

        p.setFillColor(colors.HexColor("#1f77b4"))

        

        for label, value in col1_fields:

            p.drawString(col1_x, cur_y, label)

            p.setFont("Helvetica", 10)

            p.setFillColor(colors.black)

            for subline in value.split("\n"):

                p.drawString(col1_x + value_offset, cur_y, subline)

                cur_y -= line_height

            p.setFont("Helvetica-Bold", 11)

            p.setFillColor(colors.HexColor("#1f77b4"))



        # Column 2 (Middle) - VERTICALLY ALIGNED WITH QR CODE

        col2_x = margin + left_margin_col + col_width + left_margin_col + 8

        cur_y = centered_start_y

        p.setFont("Helvetica-Bold", 11)

        p.setFillColor(colors.HexColor("#1f77b4"))

        

        for label, value in col2_fields:

            p.drawString(col2_x, cur_y, label)

            p.setFont("Helvetica", 10)

            p.setFillColor(colors.black)

            for subline in value.split("\n"):

                p.drawString(col2_x + value_offset, cur_y, subline)

                cur_y -= line_height

            p.setFont("Helvetica-Bold", 11)

            p.setFillColor(colors.HexColor("#1f77b4"))



        # Column 3 (QR Code - Right) - VERTICALLY CENTERED

        qr_data = (

            f"Student: {user.first_name} {user.last_name}\n"

            f"Matric: {index_number}\n"

            f"Programme: {profile.current_programme}\n"

            f"Level: {profile.programme_level}\n"

            f"Course: {e.course}\n"

            f"Date: {e.date.strftime('%A, %d %B %Y')}\n"

            f"Time: {e.start_time.strftime('%H:%M')} - {e.end_time.strftime('%H:%M')}\n"

            f"Building: {e.building}\nRoom: {e.room}"

        )

        qr_img = generate_logo_qr(qr_data,

                                  logo_path='static/VTIU-LOGO.png',

                                  final_size=qr_generate_size,

                                  box_size=10,

                                  border=2)

        qr_buffer = BytesIO()

        qr_img.save(qr_buffer, format='PNG')

        qr_buffer.seek(0)

        qr_reader = ImageReader(qr_buffer)

        p.drawImage(qr_reader, qr_x, qr_y, qr_display_size, qr_display_size, 

                   preserveAspectRatio=True, mask='auto')

        

        # QR Code label

        p.setFont("Helvetica", 8)

        p.setFillColor(colors.HexColor("#666666"))

        p.drawCentredString(qr_x + qr_display_size/2, qr_y - 8, "Scan for details")



        y_top = block_bottom - block_spacing



    p.showPage()

    p.save()

    buffer.seek(0)



    filename = f"exam_timetable_{index_number}.pdf"

    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')



# Teacher Assessment System

@student_bp.route('/teacher-assessment', methods=['GET', 'POST'])

@login_required

def teacher_assessment():

    """Teacher assessment form (tertiary students by programme/level)"""

    if not current_user.is_student:

        abort(403)



    # Active assessment period

    period = TeacherAssessmentPeriod.query.filter_by(is_active=True).first()

    if not period:

        flash("Teacher assessment is currently closed.", "warning")

        return redirect(url_for('student.dashboard'))



    profile = StudentProfile.query.filter_by(user_id=current_user.user_id).first()

    if not profile:

        abort(404)



    # Get teachers teaching courses in this student's programme level

    teachers = (

        db.session.query(

            User,

            db.func.string_agg(Course.name, ', ').label('courses')

        )

        .join(TeacherProfile, TeacherProfile.user_id == User.user_id)

        .join(TeacherCourseAssignment, TeacherCourseAssignment.teacher_id == TeacherProfile.id)

        .join(Course, Course.id == TeacherCourseAssignment.course_id)

        .filter(

            User.role == 'teacher',

            Course.programme_name == profile.current_programme,

            Course.programme_level == str(profile.programme_level)

        )

        .group_by(User.id)

        .all()

    )



    # Already assessed teachers

    assessed_teacher_ids = {

        a.teacher_id

        for a in TeacherAssessment.query.filter_by(

            student_id=current_user.user_id,

            period_id=period.id

        ).all()

    }



    # Calculate progress

    total_teachers = len(teachers)

    completed_count = sum(1 for teacher, _ in teachers if teacher.user_id in assessed_teacher_ids)

    progress_percent = int((completed_count / total_teachers) * 100) if total_teachers else 0



    # Get assessment questions

    questions_behavior = TeacherAssessmentQuestion.query.filter_by(

        category='teacher_behavior', is_active=True

    ).all()



    questions_response = TeacherAssessmentQuestion.query.filter_by(

        category='student_response', is_active=True

    ).all()



    # Selected teacher

    selected_teacher = None

    teacher_user_id = request.args.get('teacher')



    if teacher_user_id:

        if teacher_user_id in assessed_teacher_ids:

            flash("You have already assessed this teacher.", "info")

            return redirect(url_for('student.teacher_assessment'))



        selected_teacher = User.query.filter_by(

            user_id=teacher_user_id,

            role='teacher'

        ).first_or_404()



    # Submit assessment

    if request.method == 'POST':

        teacher_id = request.form.get('teacher_id')



        exists = TeacherAssessment.query.filter_by(

            student_id=current_user.user_id,

            teacher_id=teacher_id,

            period_id=period.id

        ).first()



        if exists:

            flash("You have already assessed this teacher.", "danger")

            return redirect(url_for('student.teacher_assessment'))



        # Create assessment record
        assessment = TeacherAssessment(
            student_id=current_user.user_id,
            teacher_id=teacher_id,
            period_id=period.id,
            class_name=f"{profile.current_programme} Level {profile.programme_level}",
            course_name=profile.current_programme or "Unknown"
        )

        # Note: Store level in course_name temporarily or create new field

        assessment.course_name = f"Level {profile.programme_level}"

        

        db.session.add(assessment)

        db.session.flush()



        for q in questions_behavior + questions_response:

            score = request.form.get(f'q_{q.id}')

            if score:

                db.session.add(

                    TeacherAssessmentAnswer(

                        assessment_id=assessment.id,

                        question_id=q.id,

                        score=int(score)

                    )

                )



        db.session.commit()

        flash("Assessment submitted successfully.", "success")

        return redirect(url_for('student.teacher_assessment'))



    return render_template(

        'student/teacher_assessment.html',

        teachers=teachers,

        assessed_teacher_ids=assessed_teacher_ids,

        selected_teacher=selected_teacher,

        questions_behavior=questions_behavior,

        questions_response=questions_response,

        total_teachers=total_teachers,

        completed_count=completed_count,

        progress_percent=progress_percent,

        programme=profile.current_programme,

        level=profile.programme_level

    )



