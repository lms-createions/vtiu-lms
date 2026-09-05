from enum import Enum

import uuid

from flask import url_for

from flask_sqlalchemy import SQLAlchemy

from flask_login import UserMixin

from werkzeug.security import generate_password_hash, check_password_hash

from datetime import datetime, timedelta

from sqlalchemy.orm import relationship, backref

from sqlalchemy.ext.hybrid import hybrid_property

import secrets, hashlib, json

from sqlalchemy.dialects.postgresql import JSON as PG_JSON 

from sqlalchemy import Column, Integer, String, Date, Time, Text

from sqlalchemy import and_, or_

from sqlalchemy.ext.hybrid import hybrid_property

from utils.extensions import db





"""

FIXED ADMIN MODEL

The issue: @property role was overriding the role column

Solution: Remove the @property role decorator and use the database column directly

"""



class Admin(db.Model, UserMixin):

    """

    Admin user model for superadmin, finance admin, academic admin, and admissions admin

    

    Admins are completely separate from regular User/Student/Teacher accounts.

    Each admin has their own username/password and specific permissions.

    """

    

    __tablename__ = 'admin'

    

    # ============================================================

    # PRIMARY KEY & IDENTITY

    # ============================================================

    

    id = db.Column(db.Integer, primary_key=True)

    """Internal database ID (auto-increment)"""

    

    public_id = db.Column(

        db.String(36), 

        unique=True, 

        nullable=False, 

        default=lambda: str(uuid.uuid4())

    )

    """Unique public ID for external references"""

    

    admin_id = db.Column(db.String(100), unique=True, nullable=True)

    """

    Unique admin identifier based on role

    Examples: FIN001, FIN002, ACA001, ADM001, SAA001

    Generated automatically when admin is created

    """

    

    user_id = db.Column(

        db.String(100), 

        db.ForeignKey('user.user_id'), 

        unique=True, 

        nullable=True

    )

    """

    Optional relationship to User table

    Can be used if admin also has a User record

    """

    

    # ============================================================

    # AUTHENTICATION

    # ============================================================

    

    username = db.Column(db.String(100), unique=True, nullable=False)

    """

    Unique username for login

    Format: firstname.lastname@admin.vtiu.edu.gh

    """

    

    email = db.Column(db.String(120), unique=True, nullable=True)

    """Email address for admin account (optional)"""

    

    password_hash = db.Column(db.String(255), nullable=False)

    """Hashed password (never store plain passwords)"""

    

    # ============================================================

    # ROLE & SUPERADMIN FLAG

    # ============================================================

    

    # ⚠️ IMPORTANT: Do NOT use @property role - use the column directly

    role = db.Column(

        db.String(50), 

        nullable=False, 

        default='finance_admin'

    )

    """

    Admin role type (DATABASE COLUMN - NOT A PROPERTY):

    - 'superadmin': Full system access, can create/manage other admins

    - 'finance_admin': Manage finances, payments, fees (💰)

    - 'academic_admin': Manage academic records, grades (📚)

    - 'admissions_admin': Manage admissions, applications (👥)

    

    ⚠️ This is a DATABASE COLUMN, not a @property!

    Access it as: admin.role

    NOT: admin.role() or @property

    """

    

    is_superadmin = db.Column(db.Boolean, default=False, nullable=False)

    """

    True if this admin is a superadmin (has full access)

    Only superadmin can create/delete other admins

    

    ✅ ALWAYS CHECK: if admin.is_superadmin

    NOT: if admin.role == 'superadmin'

    (Although role should match, is_superadmin is the explicit flag)

    """

    

    # ============================================================

    # ADMIN PERSONAL INFORMATION

    # ============================================================

    

    job_title = db.Column(db.String(100), nullable=True)

    """Job title (e.g., 'Finance Manager', 'Academic Coordinator')"""

    

    department = db.Column(db.String(100), nullable=True)

    """Department name (e.g., 'Finance', 'Academic Affairs', 'Admissions')"""

    

    phone = db.Column(db.String(20), nullable=True)

    """Office phone number"""

    

    office_location = db.Column(db.String(100), nullable=True)

    """Office building/room location"""

    

    notes = db.Column(db.Text, nullable=True)

    """Additional notes about this admin account"""

    

    # ============================================================

    # PROFILE

    # ============================================================

    

    profile_picture = db.Column(

        db.String(255), 

        nullable=True, 

        default='default_avatar.png'

    )

    """Profile picture filename"""

    

    # ============================================================

    # FINANCE ADMIN PERMISSIONS (💰)

    # ============================================================

    

    can_view_finances = db.Column(db.Boolean, default=False, nullable=False)

    """Can view payment records, transactions, and financial data"""

    

    can_edit_finances = db.Column(db.Boolean, default=False, nullable=False)

    """Can modify fees, transactions, and financial records"""

    

    can_approve_payments = db.Column(db.Boolean, default=False, nullable=False)

    """Can approve or reject student fee payments"""

    

    can_manage_fees = db.Column(db.Boolean, default=False, nullable=False)

    """Can create and modify fee structures for programmes"""

    

    # ============================================================

    # ACADEMIC ADMIN PERMISSIONS (📚)

    # ============================================================

    

    can_view_academics = db.Column(db.Boolean, default=False, nullable=False)

    """Can view student grades, transcripts, and academic data"""

    

    can_edit_academics = db.Column(db.Boolean, default=False, nullable=False)

    """Can modify grades, results, and academic information"""

    

    # ============================================================

    # ADMISSIONS ADMIN PERMISSIONS (👥)

    # ============================================================

    

    can_view_admissions = db.Column(db.Boolean, default=False, nullable=False)

    """Can view student applications and admissions records"""

    

    can_edit_admissions = db.Column(db.Boolean, default=False, nullable=False)

    """Can approve or reject student applications"""

    

    # ============================================================

    # GENERAL PERMISSIONS

    # ============================================================

    

    can_manage_users = db.Column(db.Boolean, default=False, nullable=False)

    """Can create, edit, and delete user accounts (Superadmin only)"""

    

    can_view_reports = db.Column(db.Boolean, default=False, nullable=False)

    """Can view system reports and analytics"""

    

    can_export_data = db.Column(db.Boolean, default=False, nullable=False)

    """Can export system data (CSV, Excel, etc.)"""

    

    # ============================================================

    # TIMESTAMPS

    # ============================================================

    

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    """When this admin account was created"""

    

    updated_at = db.Column(

        db.DateTime, 

        default=datetime.utcnow, 

        onupdate=datetime.utcnow,

        nullable=False

    )

    """When this admin account was last updated"""

    

    last_login = db.Column(db.DateTime, nullable=True)

    """When this admin last logged in"""

    

    date_appointed = db.Column(db.DateTime, nullable=True)

    """When this admin was appointed to their position"""

    

    # ============================================================

    # RELATIONSHIPS

    # ============================================================

    

    user = db.relationship(

        'User',

        foreign_keys=[user_id],

        backref='admin_profile',

        uselist=False

    )

    """Optional relationship to User table"""

    

    # ============================================================

    # FLASK-LOGIN REQUIRED METHODS

    # ============================================================

    

    def get_id(self):

        """

        Return the admin's unique ID for Flask-Login

        Format: admin:public_id

        """

        return f"admin:{self.public_id}"

    

    @property

    def is_authenticated(self):

        """Admin is authenticated if they have a valid account"""

        return True

    

    @property

    def is_active(self):

        """Admin account is active"""

        return True

    

    @property

    def is_anonymous(self):

        """Admin is not anonymous"""

        return False

    

    # ============================================================

    # PASSWORD MANAGEMENT

    # ============================================================

    

    def set_password(self, password: str) -> None:

        """Hash and set admin password"""

        self.password_hash = generate_password_hash(password)

    

    def check_password(self, password: str) -> bool:

        """Verify password against hash"""

        return check_password_hash(self.password_hash, password)

    

    # ============================================================

    # ROLE PROPERTIES (✅ THESE ARE OK - they check the role column)

    # ============================================================

    

    @property

    def is_finance_admin(self) -> bool:

        """Check if admin is finance admin"""

        return self.role == 'finance_admin'

    

    @property

    def is_academic_admin(self) -> bool:

        """Check if admin is academic admin"""

        return self.role == 'academic_admin'

    

    @property

    def is_admissions_admin(self) -> bool:

        """Check if admin is admissions admin"""

        return self.role == 'admissions_admin'

    

    @property

    def is_admin(self) -> bool:

        """Always True for Admin objects"""

        return True

    

    # ============================================================

    # DISPLAY PROPERTIES

    # ============================================================

    

    @property

    def display_name(self) -> str:

        """Display name for admin (username)"""

        return self.username

    

    @property

    def full_name(self) -> str:

        """

        Get full name from related User if available

        Otherwise return username

        """

        if self.user:

            return self.user.full_name

        return self.username

    

    @property

    def role_display(self) -> str:

        """Get readable role name with emoji"""

        role_map = {

            'superadmin': '⭐ Super Admin',

            'finance_admin': '💰 Finance Admin',

            'academic_admin': '📚 Academic Admin',

            'admissions_admin': '👥 Admissions Admin'

        }

        return role_map.get(self.role, self.role)

    

    @property

    def profile_picture_url(self) -> str:

        """Get URL for admin's profile picture"""

        from flask import url_for

        if self.profile_picture:

            filename = self.profile_picture.replace('static/', '').replace('uploads/profile_pictures/', '')

            return url_for('static', filename=f'uploads/profile_pictures/{filename}')

        return url_for('static', filename='img/default_profile.png')

    

    # ============================================================

    # PERMISSION CHECKING METHODS

    # ============================================================

    

    def has_permission(self, permission_name: str) -> bool:

        """

        Check if admin has a specific permission

        

        Args:

            permission_name: Permission name (e.g., 'can_view_finances')

        

        Returns:

            True if admin has permission, False otherwise

        """

        if self.is_superadmin:

            return True  # Superadmin has all permissions

        return getattr(self, permission_name, False)

    

    def has_any_permission(self, *permission_names) -> bool:

        """

        Check if admin has ANY of the specified permissions

        

        Args:

            *permission_names: Permission names to check

        

        Returns:

            True if admin has at least one permission

        """

        if self.is_superadmin:

            return True

        return any(self.has_permission(perm) for perm in permission_names)

    

    def has_all_permissions(self, *permission_names) -> bool:

        """

        Check if admin has ALL of the specified permissions

        

        Args:

            *permission_names: Permission names to check

        

        Returns:

            True if admin has all permissions

        """

        if self.is_superadmin:

            return True

        return all(self.has_permission(perm) for perm in permission_names)

    

    def get_accessible_sections(self) -> list:

        """

        Get list of sections this admin can access

        

        Returns:

            List of section names ['finances', 'academics', 'admissions', 'users', 'reports']

        """

        sections = []

        

        if self.is_superadmin or self.can_view_finances:

            sections.append('finances')

        

        if self.is_superadmin or self.can_view_academics:

            sections.append('academics')

        

        if self.is_superadmin or self.can_view_admissions:

            sections.append('admissions')

        

        if self.is_superadmin or self.can_manage_users:

            sections.append('users')

        

        if self.is_superadmin or self.can_view_reports:

            sections.append('reports')

        

        return sections

    

    # ============================================================

    # AUDIT & TRACKING METHODS

    # ============================================================

    

    def update_last_login(self):

        """Update last login timestamp"""

        self.last_login = datetime.utcnow()

        db.session.commit()

    

    def get_activity_summary(self) -> dict:

        """Get summary of admin's activity"""

        return {

            'admin_id': self.admin_id,

            'username': self.username,

            'role': self.role_display,

            'job_title': self.job_title,

            'department': self.department,

            'last_login': self.last_login,

            'created_at': self.created_at,

            'account_age_days': (datetime.utcnow() - self.created_at).days

        }

    

    def get_permissions_summary(self) -> dict:

        """Get summary of all permissions for this admin"""

        return {

            # Finance

            'can_view_finances': self.can_view_finances,

            'can_edit_finances': self.can_edit_finances,

            'can_approve_payments': self.can_approve_payments,

            'can_manage_fees': self.can_manage_fees,

            

            # Academic

            'can_view_academics': self.can_view_academics,

            'can_edit_academics': self.can_edit_academics,

            

            # Admissions

            'can_view_admissions': self.can_view_admissions,

            'can_edit_admissions': self.can_edit_admissions,

            

            # General

            'can_manage_users': self.can_manage_users,

            'can_view_reports': self.can_view_reports,

            'can_export_data': self.can_export_data,

        }

    

    # ============================================================

    # ROLE PRESET METHODS

    # ============================================================

    

    @classmethod

    def apply_finance_admin_preset(cls, admin):

        """Apply Finance Admin permission preset"""

        admin.can_view_finances = True

        admin.can_edit_finances = True

        admin.can_approve_payments = True

        admin.can_manage_fees = True

        admin.can_view_reports = True

        admin.can_export_data = True

        # Everything else False

        admin.can_view_academics = False

        admin.can_edit_academics = False

        admin.can_view_admissions = False

        admin.can_edit_admissions = False

        admin.can_manage_users = False

    

    @classmethod

    def apply_academic_admin_preset(cls, admin):

        """Apply Academic Admin permission preset"""

        admin.can_view_academics = True

        admin.can_edit_academics = True

        admin.can_view_reports = True

        admin.can_export_data = True

        # Everything else False

        admin.can_view_finances = False

        admin.can_edit_finances = False

        admin.can_approve_payments = False

        admin.can_manage_fees = False

        admin.can_view_admissions = False

        admin.can_edit_admissions = False

        admin.can_manage_users = False

    

    @classmethod

    def apply_admissions_admin_preset(cls, admin):

        """Apply Admissions Admin permission preset"""

        admin.can_view_admissions = True

        admin.can_edit_admissions = True

        admin.can_view_academics = True  # Read-only academics

        admin.can_view_reports = True

        admin.can_export_data = True

        # Everything else False

        admin.can_view_finances = False

        admin.can_edit_finances = False

        admin.can_approve_payments = False

        admin.can_manage_fees = False

        admin.can_edit_academics = False

        admin.can_manage_users = False

    

    @classmethod

    def apply_superadmin_preset(cls, admin):

        """Apply Super Admin permission preset (full access)"""

        admin.is_superadmin = True

        admin.can_view_finances = True

        admin.can_edit_finances = True

        admin.can_approve_payments = True

        admin.can_manage_fees = True

        admin.can_view_academics = True

        admin.can_edit_academics = True

        admin.can_view_admissions = True

        admin.can_edit_admissions = True

        admin.can_manage_users = True

        admin.can_view_reports = True

        admin.can_export_data = True

    

    # ============================================================

    # STRING REPRESENTATIONS

    # ============================================================

    

    def __repr__(self) -> str:

        """String representation of admin"""

        return f"<Admin {self.admin_id}: {self.username} ({self.role_display})>"

    

    def __str__(self) -> str:

        """User-friendly string representation"""

        return f"{self.admin_id} - {self.username} ({self.role_display})"

    

    # ============================================================

    # DICT CONVERSION (for JSON/API)

    # ============================================================

    

    def to_dict(self, include_password=False) -> dict:

        """

        Convert admin to dictionary

        

        Args:

            include_password: Include password hash (for DB operations only)

        

        Returns:

            Dictionary representation of admin

        """

        data = {

            'id': self.id,

            'admin_id': self.admin_id,

            'username': self.username,

            'email': self.email,

            'role': self.role,

            'role_display': self.role_display,

            'is_superadmin': self.is_superadmin,

            'job_title': self.job_title,

            'department': self.department,

            'phone': self.phone,

            'office_location': self.office_location,

            'profile_picture': self.profile_picture,

            'created_at': self.created_at.isoformat() if self.created_at else None,

            'updated_at': self.updated_at.isoformat() if self.updated_at else None,

            'last_login': self.last_login.isoformat() if self.last_login else None,

            'permissions': self.get_permissions_summary()

        }

        

        if include_password:

            data['password_hash'] = self.password_hash

        

        return data

    

    @classmethod

    def from_dict(cls, data: dict):

        """

        Create admin from dictionary

        

        Args:

            data: Dictionary with admin data

        

        Returns:

            Admin instance

        """

        admin = cls(

            admin_id=data.get('admin_id'),

            username=data.get('username'),

            email=data.get('email'),

            role=data.get('role', 'finance_admin'),

            is_superadmin=data.get('is_superadmin', False),

            job_title=data.get('job_title'),

            department=data.get('department'),

            phone=data.get('phone'),

            office_location=data.get('office_location'),

            notes=data.get('notes')

        )

        

        # Set password if provided

        if 'password' in data:

            admin.set_password(data['password'])

        

        return admin





# ============================================================

# USAGE EXAMPLES

# ============================================================



"""

# Create a new Finance Admin

new_admin = Admin(

    admin_id='FIN001',

    username='john.finance@admin.vtiu.edu.gh',

    email='john.finance@school.edu',

    role='finance_admin',

    job_title='Finance Manager',

    department='Finance'

)

new_admin.set_password('temporary_password_123')

Admin.apply_finance_admin_preset(new_admin)

db.session.add(new_admin)

db.session.commit()





# Check permissions

admin = Admin.query.get(1)

if admin.can_approve_payments:

    print("Can approve payments!")



if admin.has_any_permission('can_view_finances', 'can_view_academics'):

    print("Can access finances or academics")



if admin.is_superadmin:

    print("Full access!")





# Get accessible sections

sections = admin.get_accessible_sections()

print(f"Admin can access: {sections}")





# Update last login

admin.update_last_login()





# Get admin info as dict

data = admin.to_dict()

print(data)





# Create from dict

new_admin = Admin.from_dict({

    'admin_id': 'ACA001',

    'username': 'jane.academic@admin.vtiu.edu.gh',

    'email': 'jane.academic@school.edu',

    'role': 'academic_admin',

    'password': 'temp_pwd_456',

    'job_title': 'Academic Coordinator'

})

Admin.apply_academic_admin_preset(new_admin)

db.session.add(new_admin)

db.session.commit()

"""



class User(db.Model, UserMixin):   # <-- add UserMixin here

    __tablename__ = 'user'



    id = db.Column(db.Integer, primary_key=True)

    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    user_id = db.Column(db.String(20), unique=True, nullable=False)  # e.g. STD001, TCH001

    username = db.Column(db.String(100), nullable=False)

    email = db.Column(db.String(120), unique=True, nullable=True)

    first_name = db.Column(db.String(100), nullable=False)

    middle_name = db.Column(db.String(100))

    last_name = db.Column(db.String(100), nullable=False)

    role = db.Column(db.String(10), nullable=False)

    password_hash = db.Column(db.String(200), nullable=False)

    profile_picture = db.Column(db.String(255), nullable=True, default="default.png")

    last_seen = db.Column(db.DateTime, nullable=True)



    # Relationships

    student_profile = db.relationship('StudentProfile', back_populates='user', uselist=False, cascade='all, delete-orphan')



    @property

    def is_authenticated(self):

        """Return True if the user is authenticated (logged in)."""

        return True  # every User instance represents a real user



    @property

    def is_active(self):

        """Return True if this user is active and allowed to log in."""

        return True  # set False if you want to disable the account



    @property

    def is_anonymous(self):

        """Return True if this is an anonymous user (always False for real users)."""

        return False

    # PASSWORD MANAGEMENT

    

    def set_password(self, password: str):

        self.password_hash = generate_password_hash(password)



    def check_password(self, password: str) -> bool:

        return check_password_hash(self.password_hash, password)



    def get_id(self):

        return f"user:{self.public_id}"



    # ROLE PROPERTIES

    @property

    def is_student(self):

        return self.role == 'student'



    @property

    def is_teacher(self):

        return self.role == 'teacher'



    # NAME PROPERTIES

    @property

    def full_name(self):

        names = [self.first_name]

        if self.middle_name:

            names.append(self.middle_name)

        names.append(self.last_name)

        return ' '.join(names)



    @property

    def display_name(self):

        return self.full_name



    # PROFILE PICTURE URL

    @property

    def profile_picture_url(self):

        if self.profile_picture:

            # Normalize the stored path by removing any existing path prefixes

            filename = self.profile_picture.replace('static/', '').replace('uploads/profile_pictures/', '')

            # Remove leading/trailing slashes

            filename = filename.strip('/')

            return url_for('static', filename='uploads/profile_pictures/' + filename)

        return url_for('static', filename='img/default_profile.png')



    # UNIQUE ID

    @property

    def unique_id(self):

        """Return a universal unique ID across all user roles"""

        if hasattr(self, 'user_id'):

            return self.user_id

        return str(self.id)



    # ==================== TERTIARY EDUCATION PROPERTIES ====================

    

    @property

    def current_programme(self):

        """Return the student's current programme name."""

        if self.student_profile:

            return self.student_profile.current_programme

        return None



    @property

    def programme_level(self):

        """Return the student's programme level (100, 200, 300, or 400)."""

        if self.student_profile:

            return self.student_profile.programme_level

        return None



    @property

    def study_format(self):

        """Return the student's study format (Regular, Weekend, Online)."""

        if self.student_profile:

            return self.student_profile.study_format

        return 'Regular'



    @property

    def academic_status(self):

        """Return the student's academic status."""

        if self.student_profile:

            return self.student_profile.academic_status

        return 'Active'



    @property

    def index_number(self):

        """Return the student's matriculation number."""

        if self.student_profile:

            return self.student_profile.index_number

        return None



    @property

    def programme_info_complete(self):

        """Check if programme info is complete for registration.

        For tertiary: needs programme name and level (100-400)"""

        if not self.student_profile:

            return False

        return bool(

            self.student_profile.current_programme 

            and self.student_profile.programme_level 

            and self.student_profile.programme_level in [100, 200, 300, 400]

        )



    @property

    def registration_info_complete(self):

        """Check if student has all info needed to register for courses."""

        return self.programme_info_complete and bool(self.student_profile.index_number)



class PasswordResetRequest(db.Model):

    __tablename__ = 'password_reset_request'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.String(20), db.ForeignKey('user.user_id'), nullable=False)

    role = db.Column(db.String(10), nullable=False)

    status = db.Column(db.String(20), default='emailed')  

    requested_at = db.Column(db.DateTime, default=datetime.utcnow)

    email_sent_at = db.Column(db.DateTime)

    completed_at = db.Column(db.DateTime)

    

    user = db.relationship('User', backref='reset_requests')



class PasswordResetToken(db.Model):

    __tablename__ = 'password_reset_token'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.String(20), db.ForeignKey('user.user_id'), nullable=False)

    token_hash = db.Column(db.String(128), nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    expires_at = db.Column(db.DateTime, nullable=False)

    used = db.Column(db.Boolean, default=False)

    used_at = db.Column(db.DateTime)

    request_id = db.Column(db.Integer, db.ForeignKey('password_reset_request.id'))

    

    user = db.relationship('User', backref=db.backref('reset_tokens', cascade='all, delete-orphan'))

    request = db.relationship('PasswordResetRequest', backref=db.backref('tokens', cascade='all, delete-orphan'))



    @staticmethod

    def generate_for_user(user, request_obj=None, expires_in_minutes=60):

        import secrets, hashlib

        raw = secrets.token_urlsafe(48)

        token_hash = hashlib.sha256(raw.encode()).hexdigest()

        now = datetime.utcnow()

        token = PasswordResetToken(

            user_id=user.user_id,

            token_hash=token_hash,

            created_at=now,

            expires_at=now + timedelta(minutes=expires_in_minutes),

            request=request_obj

        )

        db.session.add(token)

        db.session.commit()

        return raw



    @staticmethod

    def verify(raw_token):

        import hashlib

        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        token = PasswordResetToken.query.filter_by(token_hash=token_hash).first()

        if not token:

            return None, 'invalid'

        if token.used:

            return None, 'used'

        if token.expires_at < datetime.utcnow():

            return None, 'expired'

        return token, 'ok'

        

# models.py



class ProgrammeCohort(db.Model):

    __tablename__ = 'programme_cohorts'

    id = db.Column(db.Integer, primary_key=True)

    programme_name = db.Column(db.String(255), nullable=False)

    level = db.Column(db.Integer, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    

    __table_args__ = (

        db.UniqueConstraint('programme_name', 'level', name='uq_programme_level'),

    )



class SchoolSettings(db.Model):

    __tablename__ = "school_settings"

    id = db.Column(db.Integer, primary_key=True)

    school_name = db.Column(db.String(255), nullable=False)

    current_academic_year = db.Column(db.String(20), nullable=False)

    current_semester = db.Column(db.String(20), nullable=False)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class StudentProfile(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.String(20), db.ForeignKey('user.user_id'), unique=True)

    dob = db.Column(db.Date, nullable=True)

    gender = db.Column(db.String(10))

    nationality = db.Column(db.String(50))

    religion = db.Column(db.String(50))

    address = db.Column(db.Text)

    city = db.Column(db.String(50))

    state = db.Column(db.String(50))

    postal_code = db.Column(db.String(100))

    phone = db.Column(db.String(20))

    email = db.Column(db.String(100))

    current_programme = db.Column(db.String(120), nullable=False)  # e.g., 'Midwifery', 'Medical Laboratory Technology'

    programme_level = db.Column(db.Integer, nullable=False)  # 100, 200, 300, or 400

    study_format = db.Column(db.String(20), default='Regular')  # Regular, Weekend, Online

    last_level_completed = db.Column(db.Integer, nullable=True)  # Last successfully completed level

    academic_status = db.Column(db.String(50), default='Active')  # Active, Probation, Graduated, Suspended

    vetting_status = db.Column(db.String(20), default='pending', nullable=False)

    rejection_reason = db.Column(db.Text, nullable=True)

    last_score = db.Column(db.Float, nullable=True)  # Last cumulative score

    admission_date = db.Column(db.Date, nullable=True)

    index_number = db.Column(db.String(50), unique=True, nullable=True)  # Student matriculation number

    semester = db.Column(db.String(20))  # '1' or '2'

    academic_year = db.Column(db.String(20))  # '2024/2025'

    guardian_name = db.Column(db.String(120))

    guardian_relation = db.Column(db.String(50))

    guardian_phone = db.Column(db.String(20))

    guardian_email = db.Column(db.String(100))

    guardian_address = db.Column(db.Text)



    user = db.relationship('User', back_populates='student_profile')

    bookings = db.relationship('AppointmentBooking', back_populates='student', cascade='all, delete-orphan')



    def __repr__(self):

        return f"<StudentProfile {self.index_number} - {self.current_programme} Level {self.programme_level}>"

    

    @property

    def class_group(self):

        """Returns a string representing the student's cohort, e.g., 'Midwifery 100'"""

        if self.current_programme and self.programme_level:

            return f"{self.current_programme} {self.programme_level}"

        return None

    

class TeacherProfile(db.Model):

    __tablename__ = 'teacher_profile'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.String(20), db.ForeignKey('user.user_id'), unique=True)

    employee_id = db.Column(db.String(20), unique=True, nullable=False)

    dob = db.Column(db.Date, nullable=True)

    gender = db.Column(db.String(10), nullable=True)

    nationality = db.Column(db.String(50), nullable=True)

    qualification = db.Column(db.String(100), nullable=True)

    specialization = db.Column(db.String(100), nullable=True)

    years_of_experience = db.Column(db.Integer, nullable=True)

    subjects_taught = db.Column(db.String(255), nullable=True)

    employment_type = db.Column(db.String(20), nullable=True)  # e.g., Full-Time, Part-Time

    department = db.Column(db.String(100), nullable=True)

    date_of_hire = db.Column(db.Date, nullable=True)

    office_location = db.Column(db.String(100), nullable=True)

    date_joined = db.Column(db.Date, default=datetime.utcnow)



    user = relationship('User', backref=backref('teacher_profile', uselist=False), foreign_keys=[user_id])

    slots = db.relationship('AppointmentSlot', back_populates='teacher', cascade='all, delete-orphan')



class ProgrammeFeeStructure(db.Model):

    __tablename__ = 'programme_fee_structure'



    id = db.Column(db.Integer, primary_key=True)

    programme_name = db.Column(db.String(120), nullable=False)

    programme_level = db.Column(db.String(20), nullable=False)  # 100,200,300,400

    study_format = db.Column(db.String(50), nullable=False)  # Regular, Weekend

    academic_year = db.Column(db.String(20), nullable=False)

    semester = db.Column(db.String(10), nullable=False)

    description = db.Column(db.String(255), nullable=False, default='Default')

    amount = db.Column(db.Float, nullable=False, default=0.0)



    items = db.Column(PG_JSON, nullable=False, default=dict)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)



    __table_args__ = (

        db.UniqueConstraint(

            'programme_name','programme_level','study_format',

            'academic_year','semester','description',

            name='uq_programme_fee_group'

        ),

    )



    # Helper properties for Text fallback

    @property

    def items_list(self):

        if isinstance(self.items, str):

            try:

                return json.loads(self.items)

            except:

                return []

        return self.items or []



    @items_list.setter

    def items_list(self, val):

        if isinstance(self.items, str) or not hasattr(self.items, 'keys'):

            self.items = json.dumps(val)

        else:

            self.items = val



class StudentFeeTransaction(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    academic_year = db.Column(db.String(20), nullable=False)

    semester = db.Column(db.String(10), nullable=False)

    amount = db.Column(db.Float, nullable=False)

    description = db.Column(db.String(255), nullable=False)

    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    proof_filename = db.Column(db.String(255))  # uploaded file

    is_approved = db.Column(db.Boolean, default=False)

    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'))



    student = db.relationship('User', backref='fee_transactions')

    reviewer = db.relationship('Admin', backref='approved_payments', foreign_keys=[reviewed_by_admin_id])

        

class StudentFeeBalance(db.Model):

    __tablename__ = 'student_fee_balance'



    id = db.Column(db.Integer, primary_key=True)

    student_id = db.Column(db.String(20), db.ForeignKey('user.user_id'), nullable=False)

    fee_structure_id = db.Column(db.Integer, db.ForeignKey('programme_fee_structure.id'), nullable=False)



    programme_name = db.Column(db.String(120), nullable=False)

    programme_level = db.Column(db.String(20), nullable=False)  # "100", "200", "300", "400"

    study_format = db.Column(db.String(50), nullable=False, default='Regular')  # ✅ ADD THIS



    academic_year = db.Column(db.String(20), nullable=False)

    semester = db.Column(db.String(10), nullable=False)



    amount_due = db.Column(db.Float, nullable=False)

    amount_paid = db.Column(db.Float, default=0.0)

    is_paid = db.Column(db.Boolean, default=False)

    paid_on = db.Column(db.DateTime)



    student = db.relationship('User', backref='fee_balances')

    fee_structure = db.relationship('ProgrammeFeeStructure')



    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



    @property

    def balance_remaining(self):

        return self.amount_due - (self.amount_paid or 0)



    __table_args__ = (db.UniqueConstraint('student_id', 'fee_structure_id', name='uq_student_fee_structure'),)


class FeePercentageSettings(db.Model):
    """Global fee percentage settings for base payment requirements"""
    
    __tablename__ = 'fee_percentage_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Percentage of total fees that must be paid as base amount
    base_payment_percentage = db.Column(db.Float, nullable=False, default=50.0)
    
    # Deadline for base payment (before installments allowed)
    base_payment_deadline = db.Column(db.Date, nullable=False)
    
    # Academic year this setting applies to
    academic_year = db.Column(db.String(20), nullable=False)
    
    # Whether installments are allowed after base payment
    allow_installments_after_base = db.Column(db.Boolean, default=True)
    
    # Description of the fee policy
    description = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Get active settings for current academic year
    @classmethod
    def get_active_settings(cls, academic_year):
        return cls.query.filter_by(academic_year=academic_year).first()


class Quiz(db.Model):

    __tablename__ = 'quiz'

    id = db.Column(db.Integer, primary_key=True)

    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    course_name = db.Column(db.String(100), nullable=False)

    title = db.Column(db.String(255), nullable=False)

    

    # Changed from assigned_class to programme_level

    programme_level = db.Column(db.String(50), nullable=False)  # "100", "200", "300", "400"

    programme_name = db.Column(db.String(120), nullable=True)  # Optional

    

    date = db.Column(db.Date, nullable=False)

    duration_minutes = db.Column(db.Integer, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    start_datetime = db.Column(db.DateTime, nullable=False)

    end_datetime = db.Column(db.DateTime, nullable=False)



    # **Force single attempt**

    attempts_allowed = db.Column(db.Integer, nullable=False, default=1)



    # Relationships

    questions = db.relationship('Question', backref='quiz', lazy=True, cascade="all, delete-orphan")

    submissions = db.relationship('StudentQuizSubmission', backref='quiz', lazy=True, cascade="all, delete-orphan")



    @property

    def max_score(self):

        return sum(q.points for q in self.questions or [])



    def __repr__(self):

        return f"<Quiz {self.title} Level {self.programme_level}>"





class Question(db.Model):

    __tablename__ = 'question'

    id = db.Column(db.Integer, primary_key=True)

    quiz_id = db.Column(db.Integer, db.ForeignKey('quiz.id'), nullable=False)

    text = db.Column(db.Text, nullable=False)

    points = db.Column(db.Float, default=1.0, nullable=False)

    question_type = db.Column(db.String(50), nullable=False, default="mcq")



    options = db.relationship(

        'Option',

        backref='question',

        cascade="all, delete-orphan",

        foreign_keys='Option.question_id'

    )



    correct_option_id = db.Column(db.Integer, db.ForeignKey('options.id'), nullable=True)



    @property

    def max_score(self):

        return float(self.points or 0.0)

    

class Option(db.Model):

    __tablename__ = 'options'

    

    id = db.Column(db.Integer, primary_key=True)

    question_id = db.Column(db.Integer, db.ForeignKey('question.id', ondelete='CASCADE'), nullable=False)

    text = db.Column(db.String(1000), nullable=False)

    is_correct = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    

    def __repr__(self):

        return f'<Option id={self.id} text={self.text[:50]}>'

    

class StudentAnswer(db.Model):

    __tablename__ = 'student_answers'

    

    id = db.Column(db.Integer, primary_key=True)

    attempt_id = db.Column(db.Integer, db.ForeignKey('quiz_attempt.id', ondelete='CASCADE'), nullable=False, index=True)

    question_id = db.Column(db.Integer, db.ForeignKey('question.id', ondelete='CASCADE'), nullable=False, index=True)

    quiz_id = db.Column(db.Integer, db.ForeignKey('quiz.id', ondelete='CASCADE'), nullable=False)

    student_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)

    selected_option_id = db.Column(db.Integer, db.ForeignKey('options.id', ondelete='SET NULL'), nullable=True)

    answer_text = db.Column(db.Text, nullable=True)

    is_correct = db.Column(db.Boolean, default=False)

    time_spent_seconds = db.Column(db.Integer, default=0)

    answered_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    

    attempt = db.relationship('QuizAttempt', backref='answers', lazy='joined')

    question = db.relationship('Question', backref='student_answers')

    quiz = db.relationship('Quiz', backref='student_answers')

    student = db.relationship('User', backref='quiz_answers')

    selected_option = db.relationship('Option', backref='selected_by_students')

    

    __table_args__ = (

        db.UniqueConstraint('attempt_id', 'question_id', name='uq_attempt_question'),

        db.Index('ix_student_answers_attempt_id', 'attempt_id'),

        db.Index('ix_student_answers_student_id', 'student_id'),

        db.Index('ix_student_answers_quiz_id', 'quiz_id'),

    )

    

    def __repr__(self):

        return f'<StudentAnswer attempt={self.attempt_id} q={self.question_id}>'

    

    @property

    def answer_value(self):

        return self.selected_option_id if self.selected_option_id is not None else self.answer_text

    

    @property

    def is_answered(self):

        return self.selected_option_id is not None or self.answer_text is not None

    

    def to_dict(self):

        return {

            'id': self.id,

            'attempt_id': self.attempt_id,

            'question_id': self.question_id,

            'quiz_id': self.quiz_id,

            'student_id': self.student_id,

            'selected_option_id': self.selected_option_id,

            'answer_text': self.answer_text,

            'time_spent_seconds': self.time_spent_seconds,

            'answered_at': self.answered_at.isoformat() if self.answered_at else None,

        }

            

    def mark_correct(self, points=None):

        """Mark this answer as correct and optionally award points"""

        self.is_correct = True

        if points is not None:

            self.points_earned = points

    

    def mark_incorrect(self):

        """Mark this answer as incorrect"""

        self.is_correct = False

        self.points_earned = 0.0



from datetime import datetime

from utils.extensions import db





class QuizAttempt(db.Model):

    __tablename__ = "quiz_attempt"



    id = db.Column(db.Integer, primary_key=True)

    quiz_id = db.Column(db.Integer, db.ForeignKey("quiz.id"), nullable=False)

    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    score = db.Column(db.Float, default=0)

    max_score = db.Column(db.Float, default=0)

    is_submitted = db.Column(db.Boolean, default=False)

    is_graded = db.Column(db.Boolean, default=False)

    started_at = db.Column(db.DateTime, default=datetime.utcnow)

    submitted_at = db.Column(db.DateTime)

    graded_at = db.Column(db.DateTime)



    __table_args__ = (

        db.UniqueConstraint("quiz_id", "student_id", name="unique_quiz_attempt"),

    )



    quiz = db.relationship("Quiz", backref="attempts")

    student = db.relationship("User", backref="quiz_attempts")



class StudentQuizSubmission(db.Model):

    __tablename__ = 'student_quiz_submissions'

    id = db.Column(db.Integer, primary_key=True)

    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    quiz_id = db.Column(db.Integer, db.ForeignKey('quiz.id'), nullable=False)

    score = db.Column(db.Float)

    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)



    student = db.relationship('User', backref='quiz_submissions')

        

class Assignment(db.Model):

    __tablename__ = 'assignments'

    id = db.Column(db.Integer, primary_key=True)

    course_name = db.Column(db.String(100), nullable=False)

    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    title = db.Column(db.String(150), nullable=False)

    description = db.Column(db.Text)

    instructions = db.Column(db.Text)

    programme_level = db.Column(db.String(50), nullable=False)  # "100", "200", "300", "400"

    programme_name = db.Column(db.String(120), nullable=True)  # Optional: specific programme

    due_date = db.Column(db.DateTime, nullable=False)

    filename = db.Column(db.String(200))

    original_name = db.Column(db.String(200))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    max_score = db.Column(db.Float, nullable=False)



    # Relationships - FIXED: removed backref='assignment' to avoid conflict

    course = db.relationship('Course', backref='assignments')

    student_submissions = db.relationship('AssignmentSubmission', 

                                         cascade='all, delete-orphan',

                                         back_populates='assignment')



    def __repr__(self):

        return f"<Assignment {self.title} for Level {self.programme_level}>"



class AssignmentSubmission(db.Model):

    __tablename__ = 'assignment_submissions'

    id = db.Column(db.Integer, primary_key=True)

    assignment_id = db.Column(db.Integer, db.ForeignKey('assignments.id'), nullable=False)

    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    filename = db.Column(db.String(255), nullable=False)

    original_name = db.Column(db.String(255), nullable=False)

    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

    score = db.Column(db.Float, nullable=True)

    feedback = db.Column(db.Text, nullable=True)

    scored_at = db.Column(db.DateTime)

    grade_letter = db.Column(db.String(5))   # e.g. A, B+, C, etc.

    pass_fail = db.Column(db.String(10))     # e.g. Pass, Fail



    # FIXED: Use back_populates instead of backref to avoid conflicts

    student = db.relationship("User", backref="assignment_submissions")

    assignment = db.relationship("Assignment", back_populates="student_submissions")



    def __repr__(self):

        return f"<AssignmentSubmission assignment={self.assignment_id} student={self.student_id}>"



class GradingScale(db.Model):

    """

    Grade boundaries for converting numeric scores to letter grades.

    Includes context for which programme/level this applies to (if needed).

    """

    __tablename__ = 'grading_scale'

    

    id = db.Column(db.Integer, primary_key=True)

    

    # CONTEXT: Which programme/level uses this scale (optional - NULL means all)

    programme_name = db.Column(db.String(120))  # NULL = applicable to all programmes

    programme_level = db.Column(db.String(10))  # NULL = applicable to all levels

    

    # SCORE RANGE

    min_score = db.Column(db.Float, nullable=False)  # Minimum percentage (e.g., 90)

    max_score = db.Column(db.Float, nullable=False)  # Maximum percentage (e.g., 100)

    

    # GRADE DETAILS

    grade_letter = db.Column(db.String(5), nullable=False)  # A, A-, B+, B, etc.

    grade_point = db.Column(db.Float, nullable=False)  # 4.0, 3.7, 3.3, 3.0, etc.

    pass_fail = db.Column(db.String(10), nullable=False, default='PASS')  # PASS or FAIL

    description = db.Column(db.String(100))  # Optional notes

    

    # DATES

    effective_from = db.Column(db.Date)  # When this scale becomes active

    effective_to = db.Column(db.Date)  # When this scale is no longer used

    

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    

    __table_args__ = (

        db.UniqueConstraint('min_score', 'max_score', 'programme_name', 'programme_level',

                          name='uq_grade_range_programme'),

        db.Index('idx_grade_score_range', 'min_score', 'max_score'),

        db.Index('idx_grade_programme_level', 'programme_name', 'programme_level'),

    )

    

    def __repr__(self):

        prog = f" ({self.programme_name} {self.programme_level})" if self.programme_name else ""

        return f"<GradingScale {self.grade_letter}: {self.min_score}-{self.max_score}%{prog}>"

    

    def is_active(self):

        """Check if this scale is currently active"""

        today = datetime.utcnow().date()

        if self.effective_from and today < self.effective_from:

            return False

        if self.effective_to and today > self.effective_to:

            return False

        return True

    

class GradingStatus(Enum):

    NOT_STARTED = "not_started"

    IN_PROGRESS = "in_progress"

    CALCULATIONS_COMPLETE = "calculations_complete"

    PENDING_TEACHER_VERIFICATION = "pending_teacher_verification"

    TEACHER_VERIFIED = "teacher_verified"

    PENDING_RESULT_RELEASE = "pending_result_release"

    RELEASED = "released"

    

class StudentCourseGrade(db.Model):

    """Tracks aggregated grades for a student in a course"""

    __tablename__ = 'student_course_grade'

    

    id = db.Column(db.Integer, primary_key=True)

    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    academic_year = db.Column(db.String(20), nullable=False)

    semester = db.Column(db.String(10), nullable=False)

    

    # Aggregated scores before weighting

    quiz_total_score = db.Column(db.Float, nullable=True)

    quiz_max_possible = db.Column(db.Float, nullable=True)

    

    assignment_total_score = db.Column(db.Float, nullable=True)

    assignment_max_possible = db.Column(db.Float, nullable=True)

    

    exam_total_score = db.Column(db.Float, nullable=True)

    exam_max_possible = db.Column(db.Float, nullable=True)

    

    # Final weighted scores (out of 100)

    quiz_weighted_score = db.Column(db.Float, nullable=True)

    assignment_weighted_score = db.Column(db.Float, nullable=True)

    exam_weighted_score = db.Column(db.Float, nullable=True)

    

    # Final grade

    final_score = db.Column(db.Float, nullable=True)  # Total out of 100

    grade_letter = db.Column(db.String(5), nullable=True)

    grade_point = db.Column(db.Float, nullable=True)  # 4.0, 3.7, 3.3, 3.0, etc.

    pass_fail = db.Column(db.String(10), nullable=True)  # PASS or FAIL

    

    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    

    # Relationships

    student = db.relationship('User', backref='course_grades')

    course = db.relationship('Course', backref='student_grades')

    

    __table_args__ = (

        db.UniqueConstraint('student_id', 'course_id', 'academic_year', 'semester', 

                          name='uq_student_course_grade'),

    )

    

    def __repr__(self):

        return f"<StudentCourseGrade student={self.student_id} course={self.course_id} final={self.final_score}>"





class CourseMaterial(db.Model):

    __tablename__ = 'course_material'



    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(120), nullable=False)

    programme_name = db.Column(db.String(100), nullable=False)   # ✅ NEW

    programme_level = db.Column(db.String(50), nullable=False)   # ✅ NEW

    course_name = db.Column(db.String(100), nullable=False)      # ✅ KEEP

    filename = db.Column(db.String(200), nullable=False)

    original_name = db.Column(db.String(200), nullable=False)

    file_type = db.Column(db.String(20), nullable=False)

    upload_date = db.Column(db.DateTime, default=datetime.utcnow)



from sqlalchemy.sql import func



class Course(db.Model):
    __tablename__ = 'course'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=False)
    programme_name = db.Column(db.String(120), nullable=False)  # NEW
    programme_level = db.Column(db.String(20), nullable=False)  # NEW (100,200,300)
    semester = db.Column(db.String(10), nullable=False)
    credit_hours = db.Column(db.Integer, default=3)
    academic_year = db.Column(db.String(20), nullable=False)
    is_mandatory = db.Column(db.Boolean, default=False)
    registration_start = db.Column(db.DateTime)
    registration_end = db.Column(db.DateTime)

    @classmethod
    def get_registration_window(cls):
        """Return a tuple (start, end) of the global registration window."""
        result = db.session.query(
            func.min(cls.registration_start),
            func.max(cls.registration_end)
        ).one()
        return result  # (start_datetime, end_datetime)

    @classmethod
    def set_registration_window(cls, start_dt, end_dt):
        """Apply the same window to every course."""
        db.session.query(cls).update({
            cls.registration_start: start_dt,
            cls.registration_end:   end_dt
        })
        db.session.commit()


class CourseLimit(db.Model):
    __tablename__ = 'course_limit'

    id = db.Column(db.Integer, primary_key=True)
    programme_name   = db.Column(db.String(120), nullable=False)
    programme_level  = db.Column(db.String(20), nullable=False)  # 100, 200, etc
    semester         = db.Column(db.String(10), nullable=False)
    academic_year    = db.Column(db.String(20), nullable=False)
    mandatory_limit  = db.Column(db.Integer, nullable=False)
    optional_limit   = db.Column(db.Integer, nullable=False)


class StudentCourseRegistration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    academic_year = db.Column(db.String(20), nullable=False)
    semester = db.Column(db.String(10), nullable=False)

    course = db.relationship('Course', backref='registrations')
    student = db.relationship('User', backref='registered_courses')


class SemesterResultRelease(db.Model):
    """Track when results are released/locked for a semester"""
    __tablename__ = 'semester_result_release'
    id = db.Column(db.Integer, primary_key=True)
    academic_year = db.Column(db.String(20), nullable=False)
    semester = db.Column(db.String(10), nullable=False)
    is_released = db.Column(db.Boolean, default=False)
    is_locked = db.Column(db.Boolean, default=False)
    released_at = db.Column(db.DateTime, nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)

    # Who submitted this semester for vetting (user id)
    submitted_by = db.Column(db.Integer, nullable=True)
    submitted_by_name = db.Column(db.String(200), nullable=True)
    submitted_at = db.Column(db.DateTime, nullable=True)
    submitted_note = db.Column(db.Text, nullable=True)

    # JSON list of courses the submitter indicated (snapshot)
    submitted_courses = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('academic_year', 'semester',
                           name='uq_semester_release'),
    )

    def __repr__(self):
        status = 'Released' if self.is_released else 'Not Released'
        return f"<SemesterResultRelease {self.academic_year} {self.semester}: {status}>"


class TimetableEntry(db.Model):
    __tablename__ = 'timetable_entry'
    id = db.Column(db.Integer, primary_key=True)
    programme_name = db.Column(db.String(120), nullable=False)
    programme_level = db.Column(db.String(20), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    day_of_week = db.Column(db.String(10), nullable=False)  # e.g., "Monday"
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)

    course = db.relationship('Course', backref='timetable_entries')


class TeacherCourseAssignment(db.Model):
    __tablename__ = 'teacher_course_assignment'
    id         = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher_profile.id'), nullable=False)
    course_id  = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    teacher = db.relationship("TeacherProfile", backref="assignments")
    course  = db.relationship("Course")


class CourseAssessmentScheme(db.Model):
    __tablename__ = 'course_assessment_scheme'

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher_profile.id'), nullable=False)  # NEW

    programme_name = db.Column(db.String(120), nullable=False)
    programme_level = db.Column(db.String(10), nullable=False)
    course_code = db.Column(db.String(20), nullable=False)
    course_name = db.Column(db.String(255), nullable=False)
    academic_year = db.Column(db.String(20), nullable=False)
    semester = db.Column(db.String(20), nullable=False)
    scheme_start_date = db.Column(db.Date)
    scheme_end_date = db.Column(db.Date)
    quiz_weight = db.Column(db.Float, nullable=False, default=10.0)
    assignment_weight = db.Column(db.Float, nullable=False, default=30.0)
    exam_weight = db.Column(db.Float, nullable=False, default=60.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    course = db.relationship('Course', backref='assessment_scheme', uselist=False)
    teacher = db.relationship('TeacherProfile', backref='assessment_schemes')  # NEW

    __table_args__ = (
        db.Index('idx_scheme_programme_level', 'programme_name', 'programme_level'),
        db.Index('idx_scheme_academic_period', 'academic_year', 'semester'),
        db.Index('idx_scheme_course', 'course_id'),
    )

    def __repr__(self):
        return (
            f"<Scheme {self.programme_name} {self.programme_level} "
            f"{self.course_code}: Q{self.quiz_weight}% A{self.assignment_weight}% E{self.exam_weight}%>"
        )

    @property
    def total_weight(self):
        """Verify weights sum to 100"""
        return self.quiz_weight + self.assignment_weight + self.exam_weight

    def is_valid(self):
        """Check if weights are valid (should sum to 100)"""
        return abs(self.total_weight - 100.0) < 0.01

    def is_active(self):
        """Check if scheme is currently active"""
        today = datetime.utcnow().date()
        if self.scheme_start_date and today < self.scheme_start_date:
            return False
        if self.scheme_end_date and today > self.scheme_end_date:
            return False
        return True

class AttendanceRecord(db.Model):
    __tablename__ = 'attendance_record'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher_profile.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=True)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    is_present = db.Column(db.Boolean, default=False)

    student = db.relationship('User')
    teacher = db.relationship('TeacherProfile')
    course = db.relationship('Course')


class AcademicCalendar(db.Model):

    __tablename__ = 'academic_calendar'

    id = db.Column(db.Integer, primary_key=True)

    date = db.Column(db.Date, nullable=False, unique=True)

    label = db.Column(db.String(100), nullable=False)

    break_type = db.Column(db.String(50), nullable=False)  # e.g. Holiday, Exam, Midterm

    is_workday = db.Column(db.Boolean, default=False)



class AcademicYear(db.Model):
    __tablename__ = 'academic_year'
    id = db.Column(db.Integer, primary_key=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    semester_1_start = db.Column(db.Date, nullable=False)
    semester_1_end = db.Column(db.Date, nullable=False)
    semester_2_start = db.Column(db.Date, nullable=False)
    semester_2_end = db.Column(db.Date, nullable=False)


class AppointmentSlot(db.Model):
    __tablename__ = 'appointment_slot'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher_profile.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    is_booked = db.Column(db.Boolean, default=False, nullable=False)

    teacher = db.relationship('TeacherProfile', back_populates='slots')
    booking = db.relationship('AppointmentBooking', back_populates='slot', uselist=False)


class AppointmentBooking(db.Model):
    __tablename__ = 'appointment_booking'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student_profile.id'), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey('appointment_slot.id'), nullable=False)
    status = db.Column(db.String(20), default='pending', nullable=False)  # pending, approved, declined, rescheduled
    note = db.Column(db.Text)
    requested_on = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


    student = db.relationship('StudentProfile', back_populates='bookings')
    slot = db.relationship('AppointmentSlot', back_populates='booking')


# ============================
# Exam-related models
# ============================
class Exam(db.Model):
    __tablename__ = 'exams'
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    programme_level = db.Column(db.String(50), nullable=False)  # "100", "200", "300", "400"
    programme_name = db.Column(db.String(120), nullable=True)  # Optional
    duration_minutes = db.Column(db.Integer, nullable=True)
    start_datetime = db.Column(db.DateTime, nullable=False)
    end_datetime = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    assignment_mode = db.Column(db.String(20), default='random', nullable=False)
    assignment_seed = db.Column(db.String(255), nullable=True)
    
    questions = db.relationship('ExamQuestion', backref='exam', cascade="all, delete-orphan")
    sets = db.relationship("ExamSet", backref="exam", cascade="all, delete-orphan")
    submissions = db.relationship('ExamSubmission', backref='exam', cascade="all, delete-orphan")
    course = db.relationship('Course', backref='exams')

    def __repr__(self):
        return f"<Exam {self.title} Level {self.programme_level}>"

    @hybrid_property
    def max_score(self):
        return sum(q.marks for q in self.questions or [])


class ExamSet(db.Model):
    __tablename__ = "exam_sets"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)
    max_score = db.Column(db.Float, nullable=True)
    access_password = db.Column(db.String(128), nullable=True)

    set_questions = db.relationship("ExamSetQuestion", backref="set", cascade="all, delete-orphan")

    @property
    def password(self):
        return self.access_password

    def __repr__(self):
        return f"<ExamSet {self.name} of Exam {self.exam_id}>"

    @property
    def computed_max_score(self):
        return sum(q.question.marks or 0 for q in self.set_questions)


class ExamQuestion(db.Model):

    __tablename__ = "exam_questions"

    id = db.Column(db.Integer, primary_key=True)

    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)

    question_text = db.Column(db.Text, nullable=False)

    question_type = db.Column(db.String(20), nullable=False)  # 'mcq', 'true_false', 'subjective'

    marks = db.Column(db.Integer, nullable=False, default=1)



    options = db.relationship("ExamOption", backref="question", cascade="all, delete-orphan")

    in_sets = db.relationship("ExamSetQuestion", backref="question", cascade="all, delete-orphan")



    def __repr__(self):

        return f"<ExamQuestion {self.question_text[:30]}...>"



class ExamSetQuestion(db.Model):

    __tablename__ = 'exam_set_questions'

    id = db.Column(db.Integer, primary_key=True)

    set_id = db.Column(db.Integer, db.ForeignKey("exam_sets.id"), nullable=False)

    question_id = db.Column(db.Integer, db.ForeignKey("exam_questions.id"), nullable=False)

    order = db.Column(db.Integer, nullable=True)



    __table_args__ = (db.UniqueConstraint("set_id", "question_id", name="uix_set_question"),)



class ExamOption(db.Model):

    __tablename__ = 'exam_options'

    id = db.Column(db.Integer, primary_key=True)

    question_id = db.Column(db.Integer, db.ForeignKey('exam_questions.id'), nullable=False)

    text = db.Column(db.String(255), nullable=False)

    is_correct = db.Column(db.Boolean, default=False)



    def __repr__(self):

        return f"<ExamOption {self.text}>"



# ============================

# Attempts / Submissions

# ============================

class ExamAttempt(db.Model):

    __tablename__ = 'exam_attempts'

    id = db.Column(db.Integer, primary_key=True)

    exam_id = db.Column(db.Integer, db.ForeignKey('exams.id'), nullable=False)

    set_id = db.Column(db.Integer, db.ForeignKey('exam_sets.id'), nullable=True)

    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    start_time = db.Column(db.DateTime, default=datetime.utcnow)

    end_time = db.Column(db.DateTime, nullable=True)

    submitted = db.Column(db.Boolean, default=False)

    submitted_at = db.Column(db.DateTime, nullable=True)   # exact submission time

    score = db.Column(db.Float, nullable=True)



    exam = db.relationship("Exam", backref="attempts")

    exam_set = db.relationship("ExamSet", backref="attempts")



    def __repr__(self):

        return f"<ExamAttempt exam={self.exam_id} student={self.student_id} submitted={self.submitted}>"



class ExamSubmission(db.Model):

    __tablename__ = 'exam_submissions'

    id = db.Column(db.Integer, primary_key=True)

    exam_id = db.Column(db.Integer, db.ForeignKey('exams.id'), nullable=False)

    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    set_id = db.Column(db.Integer, db.ForeignKey('exam_sets.id'), nullable=True)

    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

    score = db.Column(db.Float, nullable=True)



    answers = db.relationship('ExamAnswer', backref='submission', cascade="all, delete-orphan")

    exam_set = db.relationship("ExamSet", backref="submissions")



    __table_args__ = (db.UniqueConstraint('exam_id', 'student_id', name='uix_exam_student'),)



    def __repr__(self):

        return f"<ExamSubmission exam={self.exam_id} student={self.student_id}>"



    @property

    def max_score(self):

        if self.exam_set:  # ✅ always prioritize the set

            return self.exam_set.computed_max_score or 0

        return 0  # if no set was assigned, don't fall back to exam pool



class ExamAnswer(db.Model):

    __tablename__ = 'exam_answers'

    id = db.Column(db.Integer, primary_key=True)

    submission_id = db.Column(db.Integer, db.ForeignKey('exam_submissions.id'), nullable=False)

    question_id = db.Column(db.Integer, db.ForeignKey('exam_questions.id'), nullable=False)

    selected_option_id = db.Column(db.Integer, db.ForeignKey('exam_options.id'), nullable=True)

    answer_text = db.Column(db.Text, nullable=True)  # for subjective answers



    def __repr__(self):

        return f"<ExamAnswer Q{self.question_id} -> Option {self.selected_option_id or 'text'}>"



class ExamTimetableEntry(db.Model):

    __tablename__ = 'exam_timetable_entries'

    id = Column(Integer, primary_key=True)

    programme_name = db.Column(db.String(120), nullable=False)

    programme_level = db.Column(db.String(20), nullable=False)

    student_index = Column(String(64), nullable=True)       # optional: student-specific entry; usually NULL

    course = Column(String(255), nullable=False)

    date = Column(Date, nullable=False)

    start_time = Column(Time, nullable=False)

    end_time = Column(Time, nullable=False)

    room = Column(String(64))

    building = Column(String(128))

    floor = Column(String(64))

    notes = Column(Text, nullable=True)



class Notification(db.Model):

    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)

    type = db.Column(db.String(50), nullable=False, index=True)  # e.g. 'assignment', 'quiz', 'exam', 'event', 'fee', 'general'

    title = db.Column(db.String(200), nullable=False)

    message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    

    # Support both user and admin senders

    sender_id = db.Column(db.String(20), nullable=True)  # Can be user_id or admin_id

    sender_type = db.Column(db.String(10), default='system', nullable=False)  # 'user', 'admin', or 'system'

    

    related_type = db.Column(db.String(50), nullable=True)  # quiz, assignment, exam, course, etc.

    related_id = db.Column(db.Integer, nullable=True)

    

    priority = db.Column(db.String(20), default='normal')  # 'low', 'normal', 'high'

    is_archived = db.Column(db.Boolean, default=False)

    

    # Define relationships with foreign_keys parameter

    user_sender = db.relationship('User', foreign_keys='Notification.sender_id', viewonly=True, lazy='joined', primaryjoin="Notification.sender_id==User.user_id")

    

    admin_sender = db.relationship('Admin', foreign_keys='Notification.sender_id', viewonly=True, lazy='joined', primaryjoin="Notification.sender_id==Admin.admin_id")

    

    recipients = db.relationship("NotificationRecipient", back_populates="notification", cascade="all, delete-orphan")

    

    @property

    def sender(self):

        """Get the sender object (User or Admin)"""

        if self.sender_type == 'admin':

            return self.admin_sender

        return self.user_sender

    

    @property

    def sender_name(self):

        """Get the sender's display name"""

        if self.sender_type == 'system':

            return 'LMS System'

        sender = self.sender

        if sender:

            return sender.display_name

        return "Unknown"





class NotificationRecipient(db.Model):

    __tablename__ = 'notification_recipients'

    id = db.Column(db.Integer, primary_key=True)

    notification_id = db.Column(db.Integer, db.ForeignKey('notifications.id'), nullable=False)

    user_id = db.Column(db.String(20), db.ForeignKey('user.user_id'), nullable=False)

    is_read = db.Column(db.Boolean, default=False)

    read_at = db.Column(db.DateTime, nullable=True)

    notification = db.relationship('Notification', back_populates='recipients')



    user = db.relationship('User', backref='notifications_received')



class NotificationPreference(db.Model):

    """User notification preferences and settings"""

    __tablename__ = 'notification_preferences'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.String(20), db.ForeignKey('user.user_id'), nullable=False, unique=True)

    

    # Channel preferences

    email_enabled = db.Column(db.Boolean, default=True)

    in_app_enabled = db.Column(db.Boolean, default=True)

    

    # Notification type preferences (JSON: type -> enabled)

    enabled_types = db.Column(PG_JSON, default=dict)  # JSON dict of enabled notification types

    

    # Frequency settings

    digest_enabled = db.Column(db.Boolean, default=False)  # Daily digest instead of individual emails

    digest_time = db.Column(db.String(5), default='08:00')  # HH:MM format

    

    # Quiet hours

    quiet_hours_enabled = db.Column(db.Boolean, default=False)

    quiet_start = db.Column(db.String(5), default='22:00')  # HH:MM

    quiet_end = db.Column(db.String(5), default='08:00')

    

    # Mute settings

    muted_until = db.Column(db.DateTime, nullable=True)  # Mute all until this time

    

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    

    user = db.relationship('User', backref='notification_preferences', uselist=False)

    

    def is_type_enabled(self, notification_type):

        """Check if a notification type is enabled"""

        try:

            enabled = json.loads(self.enabled_types) if self.enabled_types else {}

            # Default to True if not explicitly set to False

            return enabled.get(notification_type, True)

        except:

            return True

    

    def set_type_enabled(self, notification_type, enabled):

        """Enable/disable a notification type"""

        try:

            type_dict = json.loads(self.enabled_types) if self.enabled_types else {}

            type_dict[notification_type] = enabled

            self.enabled_types = json.dumps(type_dict)

        except:

            pass



class Meeting(db.Model):

    __tablename__ = 'meetings'

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200), nullable=False)

    description = db.Column(db.Text, default='')

    host_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    meeting_code = db.Column(db.String(80), unique=True, index=True, nullable=False)

    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    scheduled_start = db.Column(db.DateTime, nullable=True)

    scheduled_end = db.Column(db.DateTime, nullable=True)

    join_url = db.Column(db.String(500))   # <-- Zoom join URL

    start_url = db.Column(db.String(500))  # <-- Zoom host start URL

    created_at = db.Column(db.DateTime, default=datetime.utcnow)



    host = db.relationship('User', backref='meetings')

    course = db.relationship('Course', backref='meetings')

    recordings = db.relationship('Recording', backref='meeting', lazy='dynamic')



class Recording(db.Model):

    __tablename__ = 'recordings'

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(255), nullable=False)

    url = db.Column(db.String(500), nullable=False)  # local path or streaming URL

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    course_id = db.Column(db.Integer, db.ForeignKey('course.id'))

    meeting_id = db.Column(db.Integer, db.ForeignKey('meetings.id'))



    teacher = db.relationship('User', backref='recordings')

    course = db.relationship('Course', backref='recordings')



class Conversation(db.Model):

    __tablename__ = "conversation"

    id = db.Column(db.Integer, primary_key=True)

    type = db.Column(db.String(20), nullable=False)  # direct | broadcast | class

    meta_json = db.Column(PG_JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



    participants = db.relationship("ConversationParticipant", backref="conversation", cascade="all, delete-orphan")

    messages = db.relationship("Message", backref="conversation", cascade="all, delete-orphan", order_by="Message.created_at.asc()")



    def get_meta(self):

        return json.loads(self.meta_json or "{}")



    def set_meta(self, data: dict):

        self.meta_json = json.dumps(data) if data else None



class ConversationParticipant(db.Model):

    __tablename__ = "conversation_participant"

    id = db.Column(db.Integer, primary_key=True)

    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False)

    user_public_id = db.Column(db.String(36), nullable=False)  # <- UUID string

    user_role = db.Column(db.String(20), nullable=False)  # 'student','teacher','admin'

    is_group_admin = db.Column(db.Boolean, default=False, nullable=False)

    can_add_members = db.Column(db.Boolean, default=False, nullable=False)

    can_remove_members = db.Column(db.Boolean, default=False, nullable=False)

    can_rename_group = db.Column(db.Boolean, default=False, nullable=False)

    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    last_read_at = db.Column(db.DateTime, nullable=True)



    __table_args__ = (db.UniqueConstraint("conversation_id", "user_public_id", "user_role", name="uq_conv_user_role_pub"),)



    @property

    def participant_obj(self):

        if self.user_role == 'admin':

            return Admin.query.filter_by(public_id=self.user_public_id).first()

        return User.query.filter_by(public_id=self.user_public_id).first()



class Message(db.Model):

    __tablename__ = "message"

    id = db.Column(db.Integer, primary_key=True)

    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False)

    sender_public_id = db.Column(db.String(36), nullable=False)    # <- UUID string

    sender_role = db.Column(db.String(20), nullable=False)

    content = db.Column(db.Text, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reply_to_message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)

    edited_at = db.Column(db.DateTime, nullable=True)

    edited_by = db.Column(db.String(36), nullable=True)

    is_deleted = db.Column(db.Boolean, default=False)

    deleted_at = db.Column(db.DateTime, nullable=True)

    deleted_by = db.Column(db.String(36), nullable=True)



    reply_to = db.relationship("Message", remote_side=[id], backref="replies")



    def to_dict(self):

        sender_name = None

        if self.sender_role == "admin":

            admin = Admin.query.filter_by(public_id=self.sender_public_id).first()

            if admin:

                sender_name = admin.username

        else:

            user = User.query.filter_by(public_id=self.sender_public_id).first()

            if user:

                sender_name = user.full_name



        content = self.content

        if self.is_deleted:

            # show deleted placeholder to clients

            content = "[message deleted]"



        reply_to_data = None

        if self.reply_to and not self.reply_to.is_deleted:

            reply_sender_name = None

            if self.reply_to.sender_role == "admin":

                admin = Admin.query.filter_by(public_id=self.reply_to.sender_public_id).first()

                if admin:

                    reply_sender_name = admin.username

            else:

                user = User.query.filter_by(public_id=self.reply_to.sender_public_id).first()

                if user:

                    reply_sender_name = user.full_name

            reply_to_data = {

                "id": self.reply_to.id,

                "sender_name": reply_sender_name or f"{self.reply_to.sender_role.capitalize()} {self.reply_to.sender_public_id}",

                "content": self.reply_to.content[:100] + "..." if len(self.reply_to.content) > 100 else self.reply_to.content,

                "created_at": self.reply_to.created_at.strftime("%Y-%m-%d %H:%M:%S"),

            }



        return {

            "id": self.id,

            "conversation_id": self.conversation_id,

            "sender_public_id": self.sender_public_id,

            "sender_role": self.sender_role,

            "sender_name": sender_name or f"{self.sender_role.capitalize()} {self.sender_public_id}",

            "content": content,

            "raw_content": None if self.is_deleted else self.content,  # keep raw for copy if client requests

            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),

            "reply_to_message_id": self.reply_to_message_id,

            "reply_to": reply_to_data,

            "edited_at": self.edited_at.strftime("%Y-%m-%d %H:%M:%S") if self.edited_at else None,

            "edited_by": self.edited_by,

            "is_deleted": bool(self.is_deleted),

            "deleted_at": self.deleted_at.strftime("%Y-%m-%d %H:%M:%S") if self.deleted_at else None,

            "deleted_by": self.deleted_by,

        }



class MessageReaction(db.Model):

    __tablename__ = "message_reaction"

    id = db.Column(db.Integer, primary_key=True)

    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=False)

    user_public_id = db.Column(db.String(36), nullable=False)

    emoji = db.Column(db.String(10), nullable=False)  # e.g., "👍"

    created_at = db.Column(db.DateTime, default=datetime.utcnow)



    def to_dict(self):

        return {

            "id": self.id,

            "message_id": self.message_id,

            "user_public_id": self.user_public_id,

            "emoji": self.emoji,

            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),

        }



class TeacherAssessmentPeriod(db.Model):

    __tablename__ = 'teacher_assessment_period'

    id = db.Column(db.Integer, primary_key=True)

    academic_year = db.Column(db.String(20), nullable=False)

    semester = db.Column(db.String(20), nullable=False)

    is_active = db.Column(db.Boolean, default=False)

    start_date = db.Column(db.Date, nullable=False)

    end_date = db.Column(db.Date, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)



class TeacherAssessmentQuestion(db.Model):

    __tablename__ = 'teacher_assessment_question'

    id = db.Column(db.Integer, primary_key=True)

    category = db.Column(db.String(50))  

    question = db.Column(db.Text, nullable=False)

    is_active = db.Column(db.Boolean, default=True)



class TeacherAssessment(db.Model):

    __tablename__ = 'teacher_assessment'

    id = db.Column(db.Integer, primary_key=True)

    student_id = db.Column(db.String(20), db.ForeignKey('user.user_id'))

    teacher_id = db.Column(db.String(20), db.ForeignKey('user.user_id'))

    class_name = db.Column(db.String(50))

    course_name = db.Column(db.String(100))

    period_id = db.Column(db.Integer, db.ForeignKey('teacher_assessment_period.id'))

    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)



    __table_args__ = (

        db.UniqueConstraint(

            'student_id', 'teacher_id', 'course_name', 'period_id',

            name='unique_teacher_assessment'

        ),

    )



class TeacherAssessmentAnswer(db.Model):

    __tablename__ = 'teacher_assessment_answer'

    id = db.Column(db.Integer, primary_key=True)

    assessment_id = db.Column(db.Integer, db.ForeignKey('teacher_assessment.id'))

    question_id = db.Column(db.Integer, db.ForeignKey('teacher_assessment_question.id'))

    score = db.Column(db.Integer)  



    assessment = db.relationship('TeacherAssessment', backref='answers')

    question = db.relationship('TeacherAssessmentQuestion')





class StudentPromotion(db.Model):

    """

    Track all student promotions for audit trail

    

    Every time a student is promoted to the next level,

    a record is created here with full details

    """

    

    __tablename__ = 'student_promotion'

    

    id = db.Column(db.Integer, primary_key=True)

    student_id = db.Column(db.String(50), db.ForeignKey('user.user_id'), nullable=False, index=True)    

    promoted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    from_level = db.Column(db.String(10), nullable=False)    

    to_level = db.Column(db.String(10), nullable=False)    

    gpa = db.Column(db.Float, nullable=False)

    academic_status = db.Column(db.String(50), nullable=False)

    academic_year = db.Column(db.String(10), nullable=False, index=True)

    promoted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    notes = db.Column(db.Text, nullable=True)

    

    student = db.relationship('User', foreign_keys=[student_id], backref='promotions_received')

    promoted_by_admin = db.relationship('User', foreign_keys=[promoted_by], backref='promotions_given')

    

    def __repr__(self):

        return f'<StudentPromotion {self.student_id}: {self.from_level}→{self.to_level} ({self.academic_year})>'

    

    def to_dict(self):

        """Convert to dictionary for JSON responses"""

        return {

            'id': self.id,

            'student_id': self.student_id,

            'student_name': self.student.full_name if self.student else 'Unknown',

            'from_level': self.from_level,

            'to_level': self.to_level,

            'gpa': self.gpa,

            'academic_status': self.academic_status,

            'academic_year': self.academic_year,

            'promoted_by': self.promoted_by_admin.username if self.promoted_by_admin else 'Unknown',

            'promoted_at': self.promoted_at.isoformat(),

            'notes': self.notes

        }


