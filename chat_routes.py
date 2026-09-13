from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from flask_socketio import emit, join_room
from utils.extensions import db, socketio
from models import Conversation, ConversationParticipant, Message, MessageReaction, User, Admin, StudentProfile, TeacherProfile
from datetime import datetime
import json

chat_bp = Blueprint('chat', __name__, url_prefix='/chat')

# -------------------------
# Helper functions
# -------------------------
def is_user_or_admin():
    """Check if current user is a teacher, student, or admin. Allows all authenticated users."""
    role = getattr(current_user, "role", None)
    return role in ["teacher", "student", "superadmin", "finance_admin", "academic_admin", "admissions_admin"]

def resolve_person_by_public_id(pub_id):
    """Return (model_instance, role_string) or (None, None). Checks both User and Admin tables."""
    if not pub_id:
        return None, None
    
    # Try to find in User table first (students/teachers)
    person = User.query.filter_by(public_id=pub_id).first()
    if person:
        return person, getattr(person, "role", "user")
    
    # Try to find in Admin table
    admin = Admin.query.filter_by(public_id=pub_id).first()
    if admin:
        return admin, admin.role  # admin.role is a database column, not a property
    
    return None, None

def add_participant_if_not_exists(conv_id, person_or_public_id, role=None):
    """
    Accept either a User instance (preferred) OR a public_id string (for external callers).
    Adds a ConversationParticipant row keyed by user_public_id.
    """
    if not person_or_public_id:
        return

    if hasattr(person_or_public_id, 'public_id'):
        user_public_id = getattr(person_or_public_id, 'public_id')
        user_role = role or getattr(person_or_public_id, 'role', 'user')
    else:
        user_public_id = str(person_or_public_id)
        if role:
            user_role = role
        else:
            resolved_person, resolved_role = resolve_person_by_public_id(user_public_id)
            user_role = resolved_role or 'user'

    exists = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=user_public_id
    ).first()
    if not exists:
        db.session.add(ConversationParticipant(
            conversation_id=conv_id,
            user_public_id=user_public_id,
            user_role=user_role
        ))

def conversation_to_dict(conv, current_user_pubid):
    participants = []
    for p in conv.participants:
        person, _ = resolve_person_by_public_id(p.user_public_id)
        display_name = getattr(
            person,
            "full_name",
            getattr(person, "username", "Unknown")
        ) if person else "Unknown"

        participants.append({
            "user_public_id": p.user_public_id,
            "role": p.user_role,
            "name": display_name
        })

    last_message = conv.messages[-1].to_dict() if conv.messages else None

    last_read_at = next(
        (p.last_read_at for p in conv.participants
         if p.user_public_id == current_user_pubid),
        None
    ) or datetime.min

    # Only count messages from OTHER users as unread (not own messages)
    unread_count = sum(
        1 for m in conv.messages
        if (m.created_at or datetime.min) > last_read_at
        and m.sender_public_id != current_user_pubid
    )

    meta = conv.get_meta() or {}

    created_by_pub = meta.get("created_by")
    created_by_name = None
    if created_by_pub:
        creator, _ = resolve_person_by_public_id(created_by_pub)
        if creator:
            created_by_name = getattr(
                creator,
                "full_name",
                getattr(creator, "username", "Unknown")
            )

    return {
        "id": conv.id,
        "type": conv.type,
        "name": meta.get("name"),
        "created_by": created_by_pub,          # ✅ public id
        "created_by_name": created_by_name,    # ✅ display name
        "participants": participants,
        "last_message": last_message,
        "unread_count": unread_count,
        "updated_at": conv.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    }

def require_group_admin(conv_id):
    """Check if current user is a group admin for this conversation."""
    p = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id,
        is_group_admin=True
    ).first()
    return p

# ───────────────
# Track online users
# ───────────────
online_users = set()
sid_to_pub = {}
sid_to_class_presence = {}
class_presence_users = {}
whiteboard_scenes = {}

# ─────────────────────────
# SocketIO events
# ─────────────────────────
@socketio.on('join')
def on_join(data):
    """User joins chat (must be teacher or student)."""
    if not is_user_or_admin():
        return
    
    pub = (data or {}).get('user_id') or getattr(current_user, 'public_id', None)
    if not pub:
        return

    sid = request.sid
    sid_to_pub[sid] = pub

    online_users.add(pub)
    join_room(f"user_{pub}")

    socketio.emit('presence_update', {'user_public_id': pub, 'status': 'online'})


@socketio.on('class_presence_join')
def on_class_presence_join(data):
    """Track authenticated users currently inside a specific live class."""
    conversation_id = (data or {}).get('conversation_id')
    if not _can_access_class_board(conversation_id):
        return

    pub = getattr(current_user, 'public_id', None)
    if not pub:
        return

    presence_room = f'class_presence_{conversation_id}'
    join_room(presence_room)
    sid = request.sid
    sid_to_class_presence[sid] = (conversation_id, pub)
    users = class_presence_users.setdefault(str(conversation_id), set())
    users.add(pub)
    socketio.emit(
        'class_presence_count',
        {'conversation_id': conversation_id, 'count': len(users)},
        room=presence_room,
    )


def _can_access_class_board(conversation_id):
    """Allow only participants in the class conversation to use its board."""
    if not is_user_or_admin() or not conversation_id:
        return False
    return ConversationParticipant.query.filter_by(
        conversation_id=conversation_id,
        user_public_id=getattr(current_user, 'public_id', None),
    ).first() is not None


@socketio.on('whiteboard_join')
def on_whiteboard_join(data):
    conversation_id = (data or {}).get('conversation_id')
    if not _can_access_class_board(conversation_id):
        return
    join_room(f'whiteboard_{conversation_id}')
    emit('whiteboard_ready', {'conversation_id': conversation_id})
    emit('whiteboard_update', {
        'conversation_id': conversation_id,
        'elements': whiteboard_scenes.get(conversation_id, []),
    })


@socketio.on('whiteboard_update')
def on_whiteboard_update(data):
    conversation_id = (data or {}).get('conversation_id')
    if not _can_access_class_board(conversation_id):
        return
    elements = (data or {}).get('elements') or []
    if not isinstance(elements, list) or len(elements) > 5000:
        return
    whiteboard_scenes[conversation_id] = elements
    socketio.emit(
        'whiteboard_update',
        {'conversation_id': conversation_id, 'elements': elements},
        room=f'whiteboard_{conversation_id}',
        include_self=False,
    )

@socketio.on('disconnect')
def on_disconnect():
    """Handle user disconnect."""
    sid = request.sid
    pub = sid_to_pub.pop(sid, None)
    class_presence = sid_to_class_presence.pop(sid, None)

    if class_presence:
        conversation_id, class_pub = class_presence
        users = class_presence_users.get(str(conversation_id), set())
        users.discard(class_pub)
        if users:
            socketio.emit(
                'class_presence_count',
                {'conversation_id': conversation_id, 'count': len(users)},
                room=f'class_presence_{conversation_id}',
            )
        else:
            class_presence_users.pop(str(conversation_id), None)

    if not pub:
        pub = getattr(current_user, 'public_id', None)

    if pub and pub in online_users:
        online_users.remove(pub)

        person, _ = resolve_person_by_public_id(pub)
        if person:
            person.last_seen = datetime.utcnow()
            db.session.commit()

        socketio.emit(
            'presence_update',
            {
                'user_public_id': pub,
                'status': 'offline',
                'last_seen': datetime.utcnow().isoformat()
            }
        )

@socketio.on('send_message')
def handle_message(data):
    """Socket event for realtime messages."""
    if not is_user_or_admin():
        return
    
    conv_id = data.get('conversation_id')
    message_text = data.get('message')
    reply_to_message_id = data.get('reply_to_message_id')
    
    if not conv_id or not message_text:
        return
    
    sender_pub = getattr(current_user, 'public_id', None)
    msg = Message(
        conversation_id=conv_id,
        sender_public_id=sender_pub,
        sender_role=getattr(current_user, "role", "user"),
        content=message_text,
        reply_to_message_id=reply_to_message_id,
    )
    db.session.add(msg)
    conv = Conversation.query.get(conv_id)
    if conv:
        conv.updated_at = datetime.utcnow()
    db.session.commit()

    if conv:
        # Broadcast to Web
        for part in conv.participants:
            room = f"user_{part.user_public_id}"
            socketio.emit('new_message', {"conversation_id": conv.id, "message": msg.to_dict()}, room=room)
        
        # Bridge to Mobile (Ktor)
        try:
            from flask import current_app
            redis_url = current_app.config.get('REDIS_URL')
            if redis_url:
                r = redis.from_url(redis_url)
                # Map receiverId correctly
                receiver_id = "global"
                meta = conv.get_meta() or {}
                if meta.get("meeting_id"):
                    receiver_id = f"meeting_{meta['meeting_id']}"
                
                r.publish('vtiu_chat_broadcast', json.dumps({
                    'sender_id': sender_pub,
                    'sender_name': current_user.username if hasattr(current_user, 'username') else 'User',
                    'receiver_id': receiver_id,
                    'message': message_text,
                    'timestamp': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                }))
        except Exception as e:
            print(f"Bridge publish error: {e}")

# ─────────────────────────
# Routes
# ─────────────────────────
@chat_bp.route('/')
@login_required
def chat_home():
    """Chat home page (teachers and students only)."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied. Only teachers and students can access chat."}), 403
    
    return render_template('chat.html')

@chat_bp.route('/conversations', methods=['GET'])
@login_required
def get_conversations():
    """Get all conversations for the current user."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    conv_participants = ConversationParticipant.query.filter_by(
        user_public_id=current_user.public_id
    ).all()
    conversations = [p.conversation for p in conv_participants]
    result = [conversation_to_dict(conv, current_user.public_id) for conv in conversations]
    return jsonify(result), 200


@chat_bp.route('/programmes', methods=['GET'])
@login_required
def get_programmes():
    """Return available programme names and level choices for frontend selectors."""
    from utils.helpers import get_programme_choices, get_level_choices
    if not is_user_or_admin():
        return jsonify({'error': 'Access denied'}), 403

    programmes = [p for (p, _) in get_programme_choices()]
    levels = [lv for (lv, label) in get_level_choices()]
    return jsonify({'programmes': programmes, 'levels': levels}), 200


@chat_bp.route('/students_by_programme', methods=['GET'])
@login_required
def students_by_programme():
    """Return users (students/teachers) filtered by programme and level."""
    if not is_user_or_admin():
        return jsonify({'error': 'Access denied'}), 403

    programme = request.args.get('programme')
    level = request.args.get('level')
    if not programme or not level:
        return jsonify({'error': 'Missing parameters'}), 400

    try:
        lvl_int = int(level)
    except:
        return jsonify({'error': 'Invalid level'}), 400

    # Join User -> StudentProfile to get users in programme+level
    students = []
    from models import User, StudentProfile
    users = User.query.join(StudentProfile, User.user_id == StudentProfile.user_id) \
        .filter(StudentProfile.current_programme == programme, StudentProfile.programme_level == lvl_int) \
        .order_by(User.first_name, User.last_name) \
        .all()

    for u in users:
        students.append({'public_id': getattr(u, 'public_id', None), 'name': getattr(u, 'full_name', getattr(u, 'username', 'Unknown'))})

    return jsonify({'students': students}), 200

@chat_bp.route('/conversations/<int:conv_id>/messages', methods=['GET'])
@login_required
def get_messages(conv_id):
    """Get messages from a conversation (participant access only)."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    conv = Conversation.query.get_or_404(conv_id)
    is_participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first() is not None
    
    if not is_participant:
        return jsonify({"error": "Access denied"}), 403
    
    messages = [m.to_dict() for m in conv.messages if not m.is_deleted]
    
    # Add reactions to each message
    for msg in messages:
        reactions = MessageReaction.query.filter_by(message_id=msg['id']).all()
        msg['reactions'] = [r.to_dict() for r in reactions]
    
    return jsonify(messages), 200

@chat_bp.route('/presence/<public_id>')
@login_required
def get_presence(public_id):
    """Get presence status of a user."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    if public_id in online_users:
        return jsonify({"status": "online"})

    person, _ = resolve_person_by_public_id(public_id)
    if person and person.last_seen:
        return jsonify({
            "status": "offline",
            "last_seen": person.last_seen.isoformat()
        })

    last_msg = Message.query.filter_by(
        sender_public_id=public_id
    ).order_by(Message.created_at.desc()).first()

    return jsonify({
        "status": "offline",
        "last_seen": last_msg.created_at.isoformat() if last_msg else None
    })

@chat_bp.route('/send_dm', methods=['POST'])
@login_required
def send_dm():
    """Send a direct message to another user or admin."""
    if not is_user_or_admin():
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    data = request.json or {}
    message_text = data.get('message')
    receiver_public_id = data.get('receiver_public_id')
    reply_to_message_id = data.get('reply_to_message_id')

    if not message_text:
        return jsonify({"success": False, "error": "Empty message"}), 400
    if not receiver_public_id:
        return jsonify({"success": False, "error": "Missing receiver_public_id"}), 400

    receiver, receiver_role = resolve_person_by_public_id(receiver_public_id)
    if not receiver:
        return jsonify({"success": False, "error": "Receiver not found"}), 404
    
    # Allow messaging to: teacher, student, or admin
    # Admins not bound by programme, so programme validation only for students/teachers
    if receiver_role not in ["teacher", "student", "superadmin", "finance_admin", "academic_admin", "admissions_admin"]:
        return jsonify({"success": False, "error": "Invalid recipient role"}), 403

    my_pub = current_user.public_id
    rec_pub = receiver_public_id

    if my_pub == rec_pub:
        return jsonify({
            "success": False,
            "error": "You cannot send a message to yourself."
        }), 400

    conv = Conversation.query.filter(
        Conversation.type == 'direct',
        Conversation.participants.any(ConversationParticipant.user_public_id == my_pub),
        Conversation.participants.any(ConversationParticipant.user_public_id == rec_pub)
    ).first()

    if not conv:
        conv = Conversation(type='direct', created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(conv)
        db.session.flush()
        add_participant_if_not_exists(conv.id, current_user)
        add_participant_if_not_exists(conv.id, receiver, role=receiver_role)
        db.session.commit()

    msg = Message(
        conversation_id=conv.id,
        sender_public_id=my_pub,
        sender_role=getattr(current_user, "role", "user"),
        content=message_text,
        reply_to_message_id=reply_to_message_id
    )
    db.session.add(msg)
    conv.updated_at = datetime.utcnow()
    db.session.commit()

    for p in conv.participants:
        room = f"user_{p.user_public_id}"
        socketio.emit('new_message', {"conversation_id": conv.id, "message": msg.to_dict()}, room=room)

    return jsonify({"success": True, "conversation_id": conv.id, "message": msg.to_dict()}), 200

@chat_bp.route('/mark_read', methods=['POST'])
@login_required
def mark_read():
    """Mark conversation as read."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    data = request.json or {}
    conversation_id = data.get('conversation_id')
    conv_part = ConversationParticipant.query.filter_by(
        conversation_id=conversation_id,
        user_public_id=getattr(current_user, 'public_id', None)
    ).first()
    if conv_part:
        conv_part.last_read_at = datetime.utcnow()
        db.session.commit()
    return jsonify({"success": True}), 200

# NOTE: Programmes are provided by the `/programmes` route defined earlier
# which returns programmes and levels together. The older distinct-programme
# endpoint was removed to avoid duplicate endpoint names.

@chat_bp.route('/levels')
@login_required
def get_levels():
    """Get all programme levels (used for filtering students in chat)."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    # Get unique levels from StudentProfile
    levels = db.session.query(StudentProfile.programme_level).distinct().order_by(StudentProfile.programme_level).all()
    result = [{"level": int(l[0])} for l in levels if l[0]]
    return jsonify(result), 200

@chat_bp.route('/users')
@login_required
def get_users():
    """
    Get list of teachers, students, and admins filtered by programme and level.
    Query params:
      - role: 'teacher', 'student', or 'admin' (required)
      - programme: filter students by programme name
      - level: filter students by programme level
    """
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    role = request.args.get('role')
    programme = request.args.get('programme')
    level = request.args.get('level')

    # Allow querying teachers, students, and admins
    if role not in ['teacher', 'student', 'admin']:
        return jsonify([])

    if role == 'admin':
        # Get all admins except current user
        users = Admin.query.filter(
            Admin.public_id != current_user.public_id
        ).order_by(Admin.username).all() if hasattr(current_user, 'public_id') else []
        
        # Build response for admins
        return jsonify([
            {
                "id": u.public_id,
                "name": f"{u.username} ({u.role.replace('_', ' ').title()})"
            }
            for u in users
        ])

    elif role == 'teacher':
        # Get all teachers except current user
        users = User.query.filter(
            User.role == 'teacher',
            User.id != current_user.id
        ).order_by(User.first_name, User.last_name).all()
    
    else:  # role == 'student'
        # ✅ FIXED: Query students through StudentProfile
        # Join User with StudentProfile to filter by programme and level
        query = db.session.query(User).join(
            StudentProfile,
            User.user_id == StudentProfile.user_id
        ).filter(
            User.role == 'student',
            User.id != current_user.id
        )
        
        # Filter by programme if provided
        if programme:
            query = query.filter(StudentProfile.current_programme == programme)
        
        # Filter by level if provided
        if level:
            try:
                level_int = int(level)
                query = query.filter(StudentProfile.programme_level == level_int)
            except (ValueError, TypeError):
                pass
        
        users = query.order_by(User.first_name, User.last_name).all()

    return jsonify([
        {
            "id": u.public_id,
            "name": u.full_name
        }
        for u in users
    ])

@chat_bp.route('/conversations/<int:conv_id>/messages/<int:msg_id>/edit', methods=['POST'])
@login_required
def edit_message(conv_id, msg_id):
    """Edit your own message."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    conv = Conversation.query.get_or_404(conv_id)

    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    if not participant:
        return jsonify({"error": "Access denied"}), 403

    msg = Message.query.filter_by(id=msg_id, conversation_id=conv_id).first_or_404()

    if msg.sender_public_id != current_user.public_id:
        return jsonify({"error": "You can only edit your own message"}), 403

    data = request.json or {}
    new_content = data.get("content", "").strip()
    if not new_content:
        return jsonify({"error": "Message cannot be empty"}), 400

    msg.content = new_content
    msg.edited_at = datetime.utcnow()
    msg.edited_by = current_user.public_id

    conv.updated_at = datetime.utcnow()
    db.session.commit()

    for p in conv.participants:
        socketio.emit(
            'message_edited',
            {"conversation_id": conv.id, "message": msg.to_dict()},
            room=f"user_{p.user_public_id}"
        )

    return jsonify({"success": True, "message": msg.to_dict()}), 200

@chat_bp.route('/conversations/<int:conv_id>/messages/<int:msg_id>/delete', methods=['POST'])
@login_required
def delete_message(conv_id, msg_id):
    """Delete your own message."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    conv = Conversation.query.get_or_404(conv_id)

    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    if not participant:
        return jsonify({"error": "Access denied"}), 403

    msg = Message.query.filter_by(id=msg_id, conversation_id=conv_id).first_or_404()

    if msg.sender_public_id != current_user.public_id:
        return jsonify({"error": "You can only delete your own message"}), 403

    msg.is_deleted = True
    msg.deleted_at = datetime.utcnow()
    msg.deleted_by = current_user.public_id

    conv.updated_at = datetime.utcnow()
    db.session.commit()

    for p in conv.participants:
        socketio.emit(
            'message_deleted',
            {"conversation_id": conv.id, "message_id": msg.id},
            room=f"user_{p.user_public_id}"
        )

    return jsonify({"success": True}), 200

@chat_bp.route('/conversations/<int:conv_id>/messages/<int:msg_id>/copy', methods=['GET'])
@login_required
def copy_message(conv_id, msg_id):
    """Copy message content."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    conv = Conversation.query.get_or_404(conv_id)

    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    if not participant:
        return jsonify({"error": "Access denied"}), 403

    msg = Message.query.filter_by(id=msg_id, conversation_id=conv_id).first_or_404()

    if msg.is_deleted:
        return jsonify({"error": "Message deleted"}), 400

    return jsonify({"content": msg.content}), 200

@chat_bp.route('/conversations/<int:conv_id>/messages/<int:msg_id>/react', methods=['POST'])
@login_required
def add_reaction(conv_id, msg_id):
    """Add emoji reaction to message."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    data = request.json or {}
    emoji = data.get('emoji')
    if not emoji:
        return jsonify({"error": "Emoji required"}), 400

    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    if not participant:
        return jsonify({"error": "Access denied"}), 403

    msg = Message.query.filter_by(id=msg_id, conversation_id=conv_id).first_or_404()

    existing = MessageReaction.query.filter_by(
        message_id=msg_id,
        user_public_id=current_user.public_id,
        emoji=emoji
    ).first()
    if existing:
        return jsonify({"error": "Already reacted"}), 400

    reaction = MessageReaction(
        message_id=msg_id,
        user_public_id=current_user.public_id,
        emoji=emoji
    )
    db.session.add(reaction)
    db.session.commit()

    participants = ConversationParticipant.query.filter_by(conversation_id=conv_id).all()
    for p in participants:
        socketio.emit(
            "reaction_added",
            {"message_id": msg_id, "reaction": reaction.to_dict()},
            room=f"user_{p.user_public_id}"
        )

    return jsonify({"success": True, "reaction": reaction.to_dict()}), 200

@chat_bp.route('/conversations/<int:conv_id>/messages/<int:msg_id>/react', methods=['DELETE'])
@login_required
def remove_reaction(conv_id, msg_id):
    """Remove emoji reaction from message."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    data = request.json or {}
    emoji = data.get('emoji')
    if not emoji:
        return jsonify({"error": "Emoji required"}), 400

    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    if not participant:
        return jsonify({"error": "Access denied"}), 403

    reaction = MessageReaction.query.filter_by(
        message_id=msg_id,
        user_public_id=current_user.public_id,
        emoji=emoji
    ).first_or_404()

    db.session.delete(reaction)
    db.session.commit()

    participants = ConversationParticipant.query.filter_by(conversation_id=conv_id).all()
    for p in participants:
        socketio.emit(
            "reaction_removed",
            {"message_id": msg_id, "user_public_id": current_user.public_id, "emoji": emoji},
            room=f"user_{p.user_public_id}"
        )

    return jsonify({"success": True}), 200

@chat_bp.route(
    '/conversations/<int:conv_id>/messages/<int:msg_id>/forward',
    methods=['POST']
)
@login_required
def forward_message(conv_id, msg_id):
    """Forward a message to another conversation."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    data = request.get_json(silent=True) or {}
    target_conv_id = data.get("target_conversation_id")

    if not target_conv_id:
        return jsonify({"error": "Missing target_conversation_id"}), 400

    source_conv = Conversation.query.get_or_404(conv_id)
    target_conv = Conversation.query.get_or_404(target_conv_id)

    is_source_participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()

    is_target_participant = ConversationParticipant.query.filter_by(
        conversation_id=target_conv_id,
        user_public_id=current_user.public_id
    ).first()

    if not is_source_participant or not is_target_participant:
        return jsonify({"error": "Access denied"}), 403

    msg = Message.query.filter_by(
        id=msg_id,
        conversation_id=conv_id
    ).first_or_404()

    if msg.is_deleted:
        return jsonify({"error": "Message deleted"}), 400

    new_msg = Message(
        conversation_id=target_conv.id,
        sender_public_id=current_user.public_id,
        sender_role=getattr(current_user, "role", "user"),
        content=msg.content
    )

    db.session.add(new_msg)
    target_conv.updated_at = datetime.utcnow()
    db.session.commit()

    for p in target_conv.participants:
        socketio.emit(
            'new_message',
            {
                "conversation_id": target_conv.id,
                "message": new_msg.to_dict()
            },
            room=f"user_{p.user_public_id}"
        )

    return jsonify({"success": True}), 200

# Group chat routes
@chat_bp.route('/conversations/group/create', methods=['POST'])
@login_required
def create_group():
    """Create a new group conversation."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    members = data.get('members', [])

    if not name or not members:
        return jsonify({'error': 'Invalid input'}), 400

    conv = Conversation(type='group')
    conv.set_meta({
        "name": name,
        "created_by": current_user.public_id,
        "admins": [current_user.public_id]
    })

    db.session.add(conv)
    db.session.flush()

    add_participant_if_not_exists(conv.id, current_user)

    for pub_id in members:
        person, role = resolve_person_by_public_id(pub_id)
        if person and role in ["teacher", "student"]:
            add_participant_if_not_exists(conv.id, person, role)

    db.session.commit()

    return jsonify(conversation_to_dict(conv, current_user.public_id)), 200

@chat_bp.route('/groups/<int:conv_id>/rename', methods=['POST'])
@login_required
def rename_group(conv_id):
    """Rename a group conversation."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    admin = require_group_admin(conv_id)
    if not admin or not admin.can_rename_group:
        return jsonify({"error": "Permission denied"}), 403

    data = request.json or {}
    name = data.get("name", "").strip()

    if not name:
        return jsonify({"error": "Name required"}), 400

    conv = Conversation.query.get_or_404(conv_id)
    meta = conv.get_meta()
    meta["name"] = name
    conv.set_meta(meta)

    db.session.commit()
    return jsonify({"success": True})

@chat_bp.route('/groups/<int:conv_id>/add_member', methods=['POST'])
@login_required
def add_group_member(conv_id):
    """Add a member to a group conversation."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    admin = require_group_admin(conv_id)
    if not admin or not admin.can_add_members:
        return jsonify({"error": "Permission denied"}), 403

    data = request.json or {}
    pub_id = data.get("user_public_id")

    person, role = resolve_person_by_public_id(pub_id)
    if not person:
        return jsonify({"error": "User not found"}), 404
    
    if role not in ["teacher", "student"]:
        return jsonify({"error": "Can only add teachers and students"}), 403

    add_participant_if_not_exists(conv_id, person, role)
    db.session.commit()

    return jsonify({"success": True})

@chat_bp.route('/groups/<int:conv_id>/remove_member', methods=['POST'])
@login_required
def remove_group_member(conv_id):
    """Remove a member from a group conversation."""
    if not is_user_or_admin():
        return jsonify({"error": "Access denied"}), 403
    
    admin = require_group_admin(conv_id)
    if not admin or not admin.can_remove_members:
        return jsonify({"error": "Permission denied"}), 403

    data = request.json or {}
    pub_id = data.get("user_public_id")

    ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=pub_id
    ).delete()

    db.session.commit()
    return jsonify({"success": True})

@chat_bp.route('/conversations/<int:conv_id>/messages', methods=['POST'])
@login_required
def post_conversation_message(conv_id):
    """Post a message to a conversation."""
    if not is_user_or_admin():
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    data = request.get_json(silent=True) or {}
    message_text = (data.get('message') or '').strip()
    reply_to_message_id = data.get('reply_to_message_id')

    if not message_text:
        return jsonify({"success": False, "error": "Empty message"}), 400

    conv = Conversation.query.get_or_404(conv_id)

    is_participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first() is not None

    if not is_participant:
        return jsonify({"success": False, "error": "Access denied"}), 403

    if reply_to_message_id:
        reply_target = Message.query.filter_by(
            id=reply_to_message_id,
            conversation_id=conv_id
        ).first()
        if not reply_target:
            return jsonify({"success": False, "error": "Invalid reply target"}), 400

    msg = Message(
        conversation_id=conv.id,
        sender_public_id=current_user.public_id,
        sender_role=getattr(current_user, "role", "user"),
        content=message_text,
        reply_to_message_id=reply_to_message_id
    )

    db.session.add(msg)
    conv.updated_at = datetime.utcnow()
    db.session.commit()

    for p in conv.participants:
        socketio.emit(
            'new_message',
            {"conversation_id": conv.id, "message": msg.to_dict()},
            room=f"user_{p.user_public_id}"
        )

    return jsonify({
        "success": True,
        "conversation_id": conv.id,
        "message": msg.to_dict()
    }), 200

@chat_bp.route('/conversations/<int:conv_id>/add_members', methods=['POST'])
@login_required
def add_members_to_group(conv_id):
    """Add multiple members to a group conversation."""
    if not is_user_or_admin():
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    conv = Conversation.query.get_or_404(conv_id)
    if conv.type != 'group':
        return jsonify({"success": False, "error": "Not a group conversation"}), 400

    is_participant = any(p.user_public_id == current_user.public_id for p in conv.participants)
    if not is_participant:
        return jsonify({"success": False, "error": "Access denied"}), 403

    data = request.get_json()
    member_ids = data.get('members', [])
    if not member_ids:
        return jsonify({"success": False, "error": "No members specified"}), 400

    added = []
    for user_id in member_ids:
        person, role = resolve_person_by_public_id(str(user_id))
        if not person:
            continue
        
        if role not in ["teacher", "student"]:
            continue
        
        exists = ConversationParticipant.query.filter_by(
            conversation_id=conv_id,
            user_public_id=str(user_id)
        ).first()
        if not exists:
            db.session.add(ConversationParticipant(
                conversation_id=conv_id,
                user_public_id=str(user_id),
                user_role=role
            ))
            added.append(str(user_id))

    db.session.commit()

    added_names = []
    for uid in added:
        person, _ = resolve_person_by_public_id(uid)
        if person:
            added_names.append(getattr(person, 'full_name', getattr(person, 'username', uid)))

    if added_names:
        msg = Message(
            conversation_id=conv.id,
            sender_public_id=current_user.public_id,
            sender_role=getattr(current_user, "role", "user"),
            content=f"{getattr(current_user, 'full_name', getattr(current_user, 'username', 'Someone'))} added {', '.join(added_names)} to the group"
        )
        db.session.add(msg)
        db.session.commit()

        for p in conv.participants:
            socketio.emit(
                'new_message',
                {"conversation_id": conv.id, "message": msg.to_dict()},
                room=f"user_{p.user_public_id}"
            )

    return jsonify({"success": True, "added": added}), 200


# ========================================
# CONVERSATION CONTEXT MENU ACTIONS
# ========================================

@chat_bp.route('/mark-unread/<int:conv_id>', methods=['POST'])
@login_required
def mark_conversation_unread(conv_id):
    """Mark conversation as unread by resetting last_read_at."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Set last_read_at to before all messages to mark as unread
    participant.last_read_at = datetime.min
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/mark-read/<int:conv_id>', methods=['POST'])
@login_required
def mark_conversation_read(conv_id):
    """Mark conversation as read by updating last_read_at."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Set last_read_at to now
    participant.last_read_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/mute/<int:conv_id>', methods=['POST'])
@login_required
def mute_conversation(conv_id):
    """Mute conversation notifications."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Store mute status in metadata
    meta = conv.get_meta() or {}
    if 'muted_by' not in meta:
        meta['muted_by'] = []
    if current_user.public_id not in meta['muted_by']:
        meta['muted_by'].append(current_user.public_id)
    conv.set_meta(meta)
    
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/unmute/<int:conv_id>', methods=['POST'])
@login_required
def unmute_conversation(conv_id):
    """Unmute conversation notifications."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Remove from mute list
    meta = conv.get_meta() or {}
    if 'muted_by' in meta and current_user.public_id in meta['muted_by']:
        meta['muted_by'].remove(current_user.public_id)
    conv.set_meta(meta)
    
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/pin/<int:conv_id>', methods=['POST'])
@login_required
def pin_conversation(conv_id):
    """Pin conversation to top."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Store pin status in metadata
    meta = conv.get_meta() or {}
    if 'pinned_by' not in meta:
        meta['pinned_by'] = []
    if current_user.public_id not in meta['pinned_by']:
        meta['pinned_by'].append(current_user.public_id)
    conv.set_meta(meta)
    
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/unpin/<int:conv_id>', methods=['POST'])
@login_required
def unpin_conversation(conv_id):
    """Unpin conversation."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Remove from pin list
    meta = conv.get_meta() or {}
    if 'pinned_by' in meta and current_user.public_id in meta['pinned_by']:
        meta['pinned_by'].remove(current_user.public_id)
    conv.set_meta(meta)
    
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/archive/<int:conv_id>', methods=['POST'])
@login_required
def archive_conversation(conv_id):
    """Archive conversation."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Store archive status in metadata
    meta = conv.get_meta() or {}
    if 'archived_by' not in meta:
        meta['archived_by'] = []
    if current_user.public_id not in meta['archived_by']:
        meta['archived_by'].append(current_user.public_id)
    conv.set_meta(meta)
    
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/unarchive/<int:conv_id>', methods=['POST'])
@login_required
def unarchive_conversation(conv_id):
    """Unarchive conversation."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Remove from archive list
    meta = conv.get_meta() or {}
    if 'archived_by' in meta and current_user.public_id in meta['archived_by']:
        meta['archived_by'].remove(current_user.public_id)
    conv.set_meta(meta)
    
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/block/<int:conv_id>', methods=['POST'])
@login_required
def block_conversation(conv_id):
    """Block a conversation (for DMs only)."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Can only block DMs, not groups
    meta = conv.get_meta() or {}
    if meta.get('is_group'):
        return jsonify({"success": False, "error": "Cannot block group conversations"}), 400
    
    # Add to blocked conversations
    meta['blocked_by'] = meta.get('blocked_by', [])
    if current_user.public_id not in meta['blocked_by']:
        meta['blocked_by'].append(current_user.public_id)
    conv.set_meta(meta)
    
    db.session.commit()
    
    return jsonify({"success": True})


@chat_bp.route('/delete/<int:conv_id>', methods=['DELETE'])
@login_required
def delete_conversation(conv_id):
    """Delete a conversation (removes current user as participant or deletes entire conversation if last participant)."""
    conv = Conversation.query.get_or_404(conv_id)
    
    # Check if user is participant
    participant = ConversationParticipant.query.filter_by(
        conversation_id=conv_id,
        user_public_id=current_user.public_id
    ).first()
    
    if not participant:
        return jsonify({"success": False, "error": "Not a participant"}), 403
    
    # Remove participant
    db.session.delete(participant)
    
    # If no more participants, delete the conversation
    remaining_participants = ConversationParticipant.query.filter_by(
        conversation_id=conv_id
    ).count()
    
    if remaining_participants == 0:
        # Delete all messages
        Message.query.filter_by(conversation_id=conv_id).delete()
        # Delete conversation
        db.session.delete(conv)
    
    db.session.commit()
    
    return jsonify({"success": True})
