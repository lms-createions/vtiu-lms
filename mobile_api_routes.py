from flask import Blueprint, jsonify, request, current_app
from models import db, User, TeacherProfile, StudentProfile, Course, Meeting, CourseMaterial, Assignment, AssignmentSubmission, Quiz, StudentQuizSubmission, Question, Option, StudentAnswer, QuizAttempt, Recording, TeacherCourseAssignment, Message, Conversation, ConversationParticipant, SchoolSettings
from utils.agora import build_rtc_token
from datetime import datetime
import json

mobile_api_bp = Blueprint('mobile_api', __name__, url_prefix='/api')

# --- MOBILE WEBSOCKET HUB ---
# Track active connections for intra-process broadcasting
active_mobile_connections = {}

def init_mobile_ws(sock):
    @sock.route('/api/chat/ws/<user_id>')
    def chat_ws(ws, user_id):
        from utils.extensions import socketio
        from models import User, Conversation, Message, db
        import redis
        
        # Connect to Redis for inter-process broadcasting
        redis_url = current_app.config.get('REDIS_URL')
        r = redis.from_url(redis_url) if redis_url else None
        
        # Subscribe to chat channel
        pubsub = r.pubsub() if r else None
        if pubsub: pubsub.subscribe('mobile_chat')
        
        active_mobile_connections[user_id] = ws
        print(f"Mobile WS: User {user_id} connected")
        
        try:
            while True:
                # 1. Check for incoming messages from Mobile
                data = ws.receive(timeout=0.1)
                if data:
                    msg_json = json.loads(data)
                    # msg_json: { senderId, receiverId, message }
                    
                    # Save to DB
                    sender = User.query.filter_by(user_id=msg_json['senderId']).first()
                    if sender:
                        # Find conversation
                        receiver_id = msg_json['receiverId']
                        if receiver_id == "global":
                            conv = Conversation.query.filter_by(type="broadcast").first()
                        elif receiver_id.startswith("meeting_"):
                            m_id = int(receiver_id.split("_")[1])
                            conv = Conversation.query.filter(Conversation.meta_json.contains(f'"meeting_id": {m_id}')).first()
                        else:
                            conv = None
                            
                        if conv:
                            new_msg = Message(
                                conversation_id=conv.id,
                                sender_public_id=sender.public_id,
                                sender_role=sender.role,
                                content=msg_json['message']
                            )
                            db.session.add(new_msg)
                            db.session.commit()
                            
                            # Broadcast to Web (SocketIO)
                            socketio.emit('new_message', {
                                'conversation_id': conv.id,
                                'message': new_msg.to_dict()
                            }, room=f"broadcast" if receiver_id == "global" else None) # Simplified
                            
                            # Broadcast to other Mobile instances (Redis)
                            if r: r.publish('mobile_chat', json.dumps(msg_json))
                
                # 2. Check for incoming messages from Redis (other instances)
                if pubsub:
                    redis_msg = pubsub.get_message(ignore_subscribe_messages=True)
                    if redis_msg:
                        ws.send(redis_msg['data'])
                        
        except Exception as e:
            print(f"Mobile WS Error: {e}")
        finally:
            active_mobile_connections.pop(user_id, None)
            if pubsub: pubsub.close()

# --- AUTH ---
@mobile_api_bp.route('/login', methods=['POST'])
def mobile_login():
    """Authenticate the Android client against the shared LMS database."""
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    user_id = (data.get('user_id') or '').strip()
    password = data.get('password') or ''
    role = (data.get('role') or '').strip().lower()

    if not username or not user_id or not password:
        return jsonify({'success': False, 'message': 'username, user_id, and password are required'}), 400

    account = User.query.filter_by(user_id=user_id).first()
    if not account or account.username.lower() != username.lower() or not account.check_password(password):
        return jsonify({'success': False, 'message': 'Invalid login credentials'}), 401

    return jsonify({
        'success': True,
        'message': 'Login successful',
        'user': {
            'id': account.id,
            'user_id': account.user_id,
            'name': account.full_name,
            'role': account.role,
            'profile_picture_url': account.profile_picture,
        }
    }), 200

# --- PROFILES ---
@mobile_api_bp.route('/profile/<role>/<user_id>', methods=['GET'])
def get_profile(role, user_id):
    user = User.query.filter_by(user_id=user_id).first()
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404
    
    profile_data = {
        'user_id': user.user_id,
        'username': user.username,
        'name': user.full_name,
        'email': user.email,
        'role': user.role,
        'profile_picture_url': user.profile_picture
    }
    
    if user.student_profile:
        p = user.student_profile
        profile_data.update({
            'programme': p.current_programme,
            'level': p.programme_level,
            'index_number': p.index_number,
            'academic_status': p.academic_status
        })
    elif user.teacher_profile:
        p = user.teacher_profile
        profile_data.update({
            'employee_id': p.employee_id,
            'qualification': p.qualification,
            'specialization': p.specialization,
            'department': p.department
        })
        
    return jsonify({'success': True, 'profile': profile_data})

# --- TEACHER / STUDENT DATA ---
@mobile_api_bp.route('/teacher/classes/<user_id>', methods=['GET'])
def get_teacher_classes(user_id):
    user = User.query.filter_by(user_id=user_id).first()
    if not user or user.role != 'teacher': return jsonify([])
    profile = user.teacher_profile
    if not profile: return jsonify([])
    
    classes = []
    for a in profile.assignments:
        c = a.course
        classes.append({
            'id': c.id,
            'course_name': c.name,
            'course_code': c.code,
            'programme': c.programme_name,
            'level': str(c.programme_level)
        })
    return jsonify(classes)

@mobile_api_bp.route('/student/courses/<user_id>', methods=['GET'])
def get_student_courses(user_id):
    user = User.query.filter_by(user_id=user_id).first()
    if not user: return jsonify([])
    
    courses = []
    for reg in user.registered_courses:
        c = reg.course
        courses.append({
            'id': c.id,
            'name': c.name,
            'code': c.code,
            'level': str(c.programme_level),
            'credits': c.credit_hours
        })
    return jsonify(courses)

# --- VCLASS / MEETINGS ---
@mobile_api_bp.route('/vclass/meetings/<int:course_id>', methods=['GET'])
def get_vclass_meetings(course_id):
    meetings = Meeting.query.filter_by(course_id=course_id).order_by(Meeting.scheduled_start.desc()).all()
    result = []
    for m in meetings:
        result.append({
            'id': m.id,
            'title': m.title,
            'course_name': m.course.name,
            'teacher_name': m.host.full_name,
            'host_id': m.host_id,
            'meeting_code': m.meeting_code,
            'start': m.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_start else "",
            'end': m.scheduled_end.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_end else "",
            'is_live': True
        })
    return jsonify(result)

@mobile_api_bp.route('/vclass/meeting/<int:meeting_id>', methods=['GET'])
def get_meeting_detail(meeting_id):
    m = Meeting.query.get(meeting_id)
    if not m: return jsonify({'message': 'Meeting not found'}), 404
    return jsonify({
        'id': m.id,
        'title': m.title,
        'course_name': m.course.name,
        'teacher_name': m.host.full_name,
        'host_id': m.host_id,
        'meeting_code': m.meeting_code,
        'start': m.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_start else "",
        'end': m.scheduled_end.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_end else "",
        'is_live': True
    })

@mobile_api_bp.route('/vclass/meeting/code/<code>', methods=['GET'])
def get_meeting_by_code(code):
    m = Meeting.query.filter_by(meeting_code=code).first()
    if not m: return jsonify({'message': 'Meeting not found'}), 404
    return jsonify({
        'id': m.id,
        'title': m.title,
        'course_name': m.course.name,
        'teacher_name': m.host.full_name,
        'host_id': m.host_id,
        'meeting_code': m.meeting_code,
        'start': m.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_start else "",
        'end': m.scheduled_end.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_end else "",
        'is_live': True
    })

@mobile_api_bp.route('/student/vclass/meetings/<user_id>', methods=['GET'])
def get_student_vclass_meetings(user_id):
    user = User.query.filter_by(user_id=user_id).first()
    if not user: return jsonify([])
    course_ids = [reg.course_id for reg in user.registered_courses]
    meetings = Meeting.query.filter(Meeting.course_id.in_(course_ids)).order_by(Meeting.scheduled_start.desc()).all()
    result = []
    for m in meetings:
        result.append({
            'id': m.id,
            'title': m.title,
            'course_name': m.course.name,
            'teacher_name': m.host.full_name,
            'host_id': m.host_id,
            'meeting_code': m.meeting_code,
            'start': m.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_start else "",
            'end': m.scheduled_end.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_end else "",
            'is_live': True
        })
    return jsonify(result)

@mobile_api_bp.route('/teacher/meetings/<user_id>', methods=['GET'])
def get_teacher_meetings(user_id):
    user = User.query.filter_by(user_id=user_id).first()
    if not user: return jsonify([])
    meetings = Meeting.query.filter_by(host_id=user.id).order_by(Meeting.scheduled_start.desc()).all()
    result = []
    for m in meetings:
        result.append({
            'id': m.id,
            'title': m.title,
            'course_name': m.course.name,
            'teacher_name': user.full_name,
            'host_id': m.host_id,
            'meeting_code': m.meeting_code,
            'start': m.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_start else "",
            'end': m.scheduled_end.strftime("%Y-%m-%d %H:%M:%S") if m.scheduled_end else "",
            'is_live': True
        })
    return jsonify(result)

# --- MATERIALS & ASSIGNMENTS ---
@mobile_api_bp.route('/vclass/materials/<int:course_id>', methods=['GET'])
def get_materials(course_id):
    course = Course.query.get(course_id)
    if not course: return jsonify([])
    materials = CourseMaterial.query.filter_by(course_name=course.name).all()
    result = []
    for m in materials:
        result.append({
            'id': m.id,
            'title': m.title,
            'course_name': m.course_name,
            'filename': m.filename,
            'file_type': m.file_type,
            'date': m.upload_date.strftime("%Y-%m-%d %H:%M:%S") if m.upload_date else ""
        })
    return jsonify(result)

@mobile_api_bp.route('/vclass/assignments/<int:course_id>', methods=['GET'])
def get_vclass_assignments(course_id):
    assignments = Assignment.query.filter_by(course_id=course_id).all()
    result = []
    for a in assignments:
        result.append({
            'id': a.id,
            'title': a.title,
            'course_name': a.course_name,
            'due_date': a.due_date.strftime("%Y-%m-%d %H:%M:%S") if a.due_date else "",
            'description': a.description
        })
    return jsonify(result)

# --- CHAT ---
@mobile_api_bp.route('/chat/history/<receiver_id>', methods=['GET'])
def get_chat_history(receiver_id):
    if receiver_id == "global":
        conv = Conversation.query.filter_by(type="broadcast").first()
    elif receiver_id.startswith("meeting_"):
        m_id = int(receiver_id.split("_")[1])
        conv = Conversation.query.filter(Conversation.meta_json.contains(f'"meeting_id": {m_id}')).first()
    else:
        conv = None
        
    if not conv: return jsonify([])
    
    result = []
    for m in conv.messages[-50:]:
        result.append({
            'id': m.id,
            'sender_id': m.sender_public_id,
            'sender_name': m.sender_name,
            'receiver_id': receiver_id,
            'message': m.content,
            'timestamp': m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        })
    return jsonify(result)

# --- AGORA & WHITEBOARD ---
@mobile_api_bp.route('/vclass/agora/token/<channel_name>/<int:user_numeric_id>', methods=['GET'])
def get_agora_token(channel_name, user_numeric_id):
    app_id = current_app.config.get('AGORA_APP_ID')
    app_cert = current_app.config.get('AGORA_APP_CERTIFICATE')
    if not app_id: return jsonify({'message': 'Agora not configured'}), 500
    if not app_cert: return jsonify({'token': '', 'appId': app_id})
    try:
        user = User.query.get(user_numeric_id)
        role_str = 'host' if user and user.role == 'teacher' else 'audience'
        token = build_rtc_token(app_id, app_cert, channel_name, user_numeric_id, role_str)
        return jsonify({'token': token, 'appId': app_id})
    except Exception as e:
        return jsonify({'message': str(e)}), 500

@mobile_api_bp.route('/vclass/whiteboard/<int:meeting_id>', methods=['GET'])
def get_whiteboard_room(meeting_id):
    meeting = Meeting.query.get(meeting_id)
    if not meeting: return jsonify(None), 404
    room_uuid = meeting.whiteboard_room_uuid
    if not room_uuid:
        import uuid
        room_uuid = f"{uuid.uuid4().hex[:20]},{uuid.uuid4().hex[:22]}"
        meeting.whiteboard_room_uuid = room_uuid
        db.session.commit()
    return jsonify({
        'type': 'excalidraw',
        'roomUrl': f"https://excalidraw.com/#room={room_uuid}",
        'roomUuid': room_uuid
    })

# --- QUIZZES & EXAMS ---
@mobile_api_bp.route('/student/quizzes/<user_id>', methods=['GET'])
def get_student_quizzes(user_id):
    user = User.query.filter_by(user_id=user_id).first()
    if not user: return jsonify([])
    p = user.student_profile
    if not p: return jsonify([])
    
    quizzes = Quiz.query.filter_by(programme_name=p.current_programme, programme_level=str(p.programme_level)).all()
    result = []
    for q in quizzes:
        result.append({
            'id': q.id,
            'title': q.title,
            'course_name': q.course_name,
            'duration_minutes': q.duration_minutes,
            'start_datetime': q.start_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            'end_datetime': q.end_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            'attempts_allowed': q.attempts_allowed
        })
    return jsonify(result)

@mobile_api_bp.route('/vclass/quiz/<int:quiz_id>', methods=['GET'])
def get_quiz_detail(quiz_id):
    q = Quiz.query.get(quiz_id)
    if not q: return jsonify(None), 404
    
    questions = []
    for question in q.questions:
        options = []
        for opt in question.options:
            options.append({'id': opt.id, 'text': opt.text})
        
        questions.append({
            'id': question.id,
            'text': question.text,
            'type': question.question_type,
            'points': question.points,
            'options': options
        })
        
    return jsonify({
        'id': q.id,
        'title': q.title,
        'course_name': q.course_name,
        'duration_minutes': q.duration_minutes,
        'questions': questions
    })

# --- PERFORMANCE ---
@mobile_api_bp.route('/teacher/performance/<int:course_id>', methods=['GET'])
def get_class_performance(course_id):
    registrations = StudentCourseRegistration.query.filter_by(course_id=course_id).all()
    result = []
    for reg in registrations:
        s = reg.student
        result.append({
            'student_id': s.user_id,
            'student_name': s.full_name,
            'quiz_score': 0.0, # Aggregation needed in real use
            'assignment_score': 0.0,
            'exam_score': 0.0,
            'total_score': 0.0,
            'grade': 'N/A',
            'status': 'Active'
        })
    return jsonify(result)

# --- FINANCE ---
@mobile_api_bp.route('/fees/balance/<user_id>', methods=['GET'])
def get_fee_balance(user_id):
    from models import StudentFeeBalance
    b = StudentFeeBalance.query.filter_by(student_id=user_id).first()
    if not b: return jsonify({'amount_due': 0.0, 'amount_paid': 0.0, 'balance': 0.0})
    return jsonify({
        'amount_due': b.amount_due,
        'amount_paid': b.amount_paid,
        'balance': b.balance_remaining
    })
