"""
FINANCE ADMIN DASHBOARD ROUTE
Displays financial data: payments, fees, student balances
"""

from flask import Blueprint, render_template, request, jsonify, abort, flash, redirect, url_for
from flask_login import login_required, current_user
from models import Admin, StudentFeeBalance, StudentFeeTransaction, ProgrammeFeeStructure, StudentProfile, User, AcademicYear, TeacherProfile
from utils.extensions import db
from functools import wraps
from datetime import datetime, timedelta
from admissions.forms import CERTIFICATE_PROGRAMMES, DIPLOMA_PROGRAMMES, STUDY_FORMATS
import logging
import json
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

finance_bp = Blueprint('finance', __name__)


# ============================================================
# PERMISSION DECORATOR
# ============================================================

def require_finance_admin(f):
    """Require user to be Finance Admin or SuperAdmin"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not isinstance(current_user, Admin):
            abort(403)
        # Allow superadmin or finance_admin role
        if current_user.is_superadmin:
            return f(*args, **kwargs)
        if current_user.is_finance_admin:
            return f(*args, **kwargs)
        # Also allow if role contains 'finance'
        if current_user.role and 'finance' in current_user.role.lower():
            return f(*args, **kwargs)
        abort(403)
    return decorated


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def get_financial_summary():
    """Get comprehensive financial summary statistics"""
    try:
        # Total revenue (all approved payments)
        total_revenue = db.session.query(db.func.sum(StudentFeeTransaction.amount)).filter_by(is_approved=True).scalar() or 0
        
        # Outstanding balance (sum of all unpaid balances) - amount_due - amount_paid
        balance_query = db.session.query(
            db.func.sum(StudentFeeBalance.amount_due - db.func.coalesce(StudentFeeBalance.amount_paid, 0))
        ).filter(
            (StudentFeeBalance.amount_due - db.func.coalesce(StudentFeeBalance.amount_paid, 0)) > 0
        ).scalar() or 0
        outstanding_balance = float(balance_query) if balance_query else 0
        
        # Pending payments count (not yet reviewed)
        pending_count = StudentFeeTransaction.query.filter_by(is_approved=False).count()
        
        # Approved payments count
        approved_count = StudentFeeTransaction.query.filter_by(is_approved=True).count()
        
        # Students with debt
        students_with_debt = db.session.query(db.func.count(db.distinct(StudentFeeBalance.student_id))).filter(
            (StudentFeeBalance.amount_due - db.func.coalesce(StudentFeeBalance.amount_paid, 0)) > 0
        ).scalar() or 0
        
        # Total students with fee records
        total_students = db.session.query(db.func.count(db.distinct(StudentFeeBalance.student_id))).scalar() or 0
        
        # Collection rate
        collection_rate = 0
        if total_revenue and outstanding_balance:
            collection_rate = (total_revenue / (total_revenue + outstanding_balance)) * 100
        
        return {
            'total_revenue': float(total_revenue),
            'outstanding_balance': float(outstanding_balance),
            'pending_count': pending_count,
            'approved_count': approved_count,
            'students_with_debt': students_with_debt,
            'total_students': total_students,
            'collection_rate': round(collection_rate, 2)
        }
    except Exception as e:
        logger.exception(f"Error calculating financial summary: {e}")
        return {
            'total_revenue': 0,
            'outstanding_balance': 0,
            'pending_count': 0,
            'approved_count': 0,
            'students_with_debt': 0,
            'total_students': 0,
            'collection_rate': 0
        }


def get_recent_payments(limit=50):
    """Get recent payments with student details"""
    try:
        from models import User
        
        payments = db.session.query(
            StudentFeeTransaction.id,
            StudentFeeTransaction.amount,
            StudentFeeTransaction.status,
            StudentFeeTransaction.created_at.label('date'),
            User.user_id.label('student_id'),
            User.full_name.label('student_name')
        ).join(
            User, StudentFeeTransaction.student_id == User.user_id
        ).order_by(StudentFeeTransaction.created_at.desc()).limit(limit).all()
        
        return [
            {
                'id': p.id,
                'student_id': p.student_id,
                'student_name': p.student_name,
                'amount': float(p.amount),
                'date': p.date.strftime('%Y-%m-%d %H:%M') if p.date else '',
                'status': p.status
            }
            for p in payments
        ]
    except Exception as e:
        logger.exception(f"Error fetching payments: {e}")
        return []


def get_fee_structures():
    """Get all fee structures with item counts"""
    try:
        structures = ProgrammeFeeStructure.query.all()
        
        return [
            {
                'id': s.id,
                'programme_name': s.programme_name,
                'level': s.level,
                'total_amount': float(sum(item.amount for item in s.items)),
                'item_count': len(s.items)
            }
            for s in structures
        ]
    except Exception as e:
        logger.exception(f"Error fetching fee structures: {e}")
        return []


def get_student_balances(limit=100):
    """Get student fee balances"""
    try:
        from sqlalchemy import func
        
        # Calculate balance_remaining in SQL: amount_due - amount_paid
        balance_remaining = StudentFeeBalance.amount_due - func.coalesce(StudentFeeBalance.amount_paid, 0)
        
        balances = db.session.query(
            StudentFeeBalance.id,
            StudentFeeBalance.amount_due,
            StudentFeeBalance.amount_paid,
            balance_remaining.label('balance_remaining'),
            StudentFeeBalance.is_paid,
            StudentFeeBalance.programme_name,
            User.first_name,
            User.middle_name,
            User.last_name
        ).join(
            User, StudentFeeBalance.student_id == User.user_id
        ).order_by(StudentFeeBalance.amount_due.desc()).limit(limit).all()
        
        logger.info(f"Found {len(balances)} student balances")
        
        result = [
            {
                'id': b.id,
                'student_name': ' '.join(filter(None, [b.first_name, b.middle_name, b.last_name])),
                'programme': b.programme_name or 'Unknown',
                'amount_due': float(b.amount_due),
                'amount_paid': float(b.amount_paid or 0),
                'balance': float(b.balance_remaining or 0),
                'status': 'paid' if b.is_paid else ('partial' if (b.amount_paid or 0) > 0 else 'pending')
            }
            for b in balances
        ]
        
        logger.info(f"Returning {len(result)} formatted balances")
        return result
        
    except Exception as e:
        logger.exception(f"Error fetching balances: {e}")
        return []


# ============================================================
# ROUTES
# ============================================================

@finance_bp.route('/assign-fees', methods=['GET', 'POST'])
@login_required
@require_finance_admin
def assign_fees():
    """Assign fees to programmes and levels"""
    
    academic_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    CLASS_LEVELS = ['100', '200', '300', '400']

    if request.method == 'POST':
        programme_name = request.form.get('programme_name')
        programme_level = request.form.get('programme_level')
        study_format = request.form.get('study_format') or 'Regular'
        academic_year_id = request.form.get('academic_year')
        semester = request.form.get('semester')
        group_title = request.form.get('group_title') or 'Default'

        if not programme_name or not programme_level or not academic_year_id or not semester:
            flash("Missing required fields.", "danger")
            return redirect(url_for('finance.assign_fees'))

        academic_year_obj = AcademicYear.query.get(academic_year_id)
        academic_year_str = str(academic_year_obj.start_date.year) if academic_year_obj else str(datetime.now().year)

        descriptions = request.form.getlist('description[]')
        amounts = request.form.getlist('amount[]')
        items = []
        total = 0.0
        
        for desc, amt in zip(descriptions, amounts):
            try:
                amt_f = float(amt or 0)
                items.append({'description': desc.strip(), 'amount': round(amt_f, 2)})
                total += amt_f
            except ValueError:
                flash("Invalid amount provided.", "danger")
                return redirect(url_for('finance.assign_fees'))

        if not items:
            flash("Add at least one fee item.", "danger")
            return redirect(url_for('finance.assign_fees'))

        existing = ProgrammeFeeStructure.query.filter_by(
            programme_name=programme_name,
            programme_level=programme_level,
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
            return redirect(url_for('finance.assign_fees'))

        try:
            new_group = ProgrammeFeeStructure(
                programme_name=programme_name,
                programme_level=programme_level,
                study_format=study_format,
                academic_year=academic_year_str,
                semester=semester,
                description=group_title,
                amount=round(total, 2),
                items=json.dumps(items)
            )

            db.session.add(new_group)
            db.session.commit()
            
            if programme_level == '100':
                flash("✓ Level 100 (Freshers) fees created - will auto-assign upon admission approval.", "success")
            else:
                flash(f"✓ Level {programme_level} (Continuing Students) fees created.", "success")
            
            return redirect(url_for('finance.assign_fees'))
        except Exception as e:
            db.session.rollback()
            logger.exception(f"Error assigning fees: {e}")
            flash(f"Error: {str(e)}", "danger")
            return redirect(url_for('finance.assign_fees'))

    groups = ProgrammeFeeStructure.query.order_by(
        ProgrammeFeeStructure.programme_name,
        ProgrammeFeeStructure.programme_level,
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
        academic_years=academic_years
    )


@finance_bp.route('/edit-fee-group/<int:group_id>', methods=['GET', 'POST'])
@login_required
@require_finance_admin
def edit_fee_group(group_id):
    """Edit a fee group"""
    
    group = ProgrammeFeeStructure.query.get_or_404(group_id)
    academic_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    CLASS_LEVELS = ['100', '200', '300', '400']

    if request.method == 'POST':
        try:
            group.programme_name = request.form.get('programme_name')
            group.programme_level = request.form.get('programme_level')
            group.study_format = request.form.get('study_format') or 'Regular'
            group.semester = request.form.get('semester')
            group.description = request.form.get('group_title')

            descriptions = request.form.getlist('description[]')
            amounts = request.form.getlist('amount[]')
            items = []
            total = 0.0
            
            for desc, amt in zip(descriptions, amounts):
                amt_f = float(amt or 0)
                items.append({'description': desc.strip(), 'amount': round(amt_f, 2)})
                total += amt_f

            group.amount = round(total, 2)
            group.items = json.dumps(items)

            db.session.commit()
            flash("✓ Fee group updated successfully.", "success")
            return redirect(url_for('finance.assign_fees'))
        except Exception as e:
            db.session.rollback()
            logger.exception(f"Error updating fee group: {e}")
            flash(f"Error: {str(e)}", "danger")

    try:
        items = json.loads(group.items) if group.items else []
    except:
        items = []

    return render_template(
        'admin/finance_edit_fee_group.html',
        group=group,
        items=items,
        CERTIFICATE_PROGRAMMES=CERTIFICATE_PROGRAMMES,
        DIPLOMA_PROGRAMMES=DIPLOMA_PROGRAMMES,
        CLASS_LEVELS=CLASS_LEVELS,
        STUDY_FORMATS=STUDY_FORMATS,
        academic_years=academic_years
    )


@finance_bp.route('/delete-fee/<int:fee_id>', methods=['POST'])
@login_required
@require_finance_admin
def delete_fee(fee_id):
    """Delete a fee structure"""
    
    try:
        group = ProgrammeFeeStructure.query.get_or_404(fee_id)
        db.session.delete(group)
        db.session.commit()
        flash("✓ Fee structure deleted.", "success")
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error deleting fee: {e}")
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('finance.assign_fees'))


@finance_bp.route('/review-payments', methods=['GET'])
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


@finance_bp.route('/approve-payment/<int:txn_id>', methods=['POST'])
@login_required
@require_finance_admin
def approve_payment_route(txn_id):
    """Approve a pending payment"""
    
    txn = StudentFeeTransaction.query.get_or_404(txn_id)

    if getattr(txn, 'is_approved', False):
        flash("Payment already approved.", "warning")
        return redirect(url_for('finance.review_payments'))

    try:
        txn.is_approved = True
        txn.reviewed_by_admin_id = current_user.id if hasattr(current_user, 'id') else None
        if hasattr(txn, 'reviewed_at'):
            txn.reviewed_at = datetime.utcnow()

        # Resolve student
        student = User.query.get(txn.student_id) if txn.student_id else None
        if not student:
            flash("Student record not found for this transaction.", "danger")
            db.session.rollback()
            return redirect(url_for('finance.review_payments'))

        student_user_id = getattr(student, 'user_id', None) or str(getattr(student, 'id', None))

        # Update balance
        balance = StudentFeeBalance.query.filter_by(
            student_id=student_user_id,
            academic_year=txn.academic_year,
            semester=txn.semester
        ).first()

        if balance:
            balance.balance = max(0, balance.balance - txn.amount)
            if balance.balance <= 0:
                balance.status = 'paid'
            else:
                balance.status = 'partial'

        db.session.commit()
        flash(f"✓ Payment of GHS {txn.amount:.2f} approved.", "success")
        logger.info(f"Payment {txn_id} approved by {current_user.admin_id if hasattr(current_user, 'admin_id') else 'admin'}")
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error approving payment: {e}")
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('finance.review_payments'))


@finance_bp.route('/reject-payment/<int:txn_id>', methods=['POST'])
@login_required
@require_finance_admin
def reject_payment_route(txn_id):
    """Reject a pending payment"""
    
    txn = StudentFeeTransaction.query.get_or_404(txn_id)

    if getattr(txn, 'is_approved', False):
        flash("Cannot reject an approved payment.", "warning")
        return redirect(url_for('finance.review_payments'))

    try:
        reason = request.form.get('reason', 'No reason provided')
        txn.is_approved = False
        if hasattr(txn, 'rejection_reason'):
            txn.rejection_reason = reason
        
        db.session.commit()
        flash(f"✓ Payment rejected. Reason: {reason}", "success")
        logger.info(f"Payment {txn_id} rejected by {current_user.admin_id if hasattr(current_user, 'admin_id') else 'admin'}")
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error rejecting payment: {e}")
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('finance.review_payments'))


@finance_bp.route('/dashboard', methods=['GET'])
@login_required
@require_finance_admin
def dashboard():
    """Finance admin dashboard with comprehensive financial data"""
    
    try:
        # Get all data
        summary = get_financial_summary()
        payments = get_recent_payments(limit=50)
        fees = get_fee_structures()
        balances = get_student_balances(limit=100)
        
        # Quick action counts
        pending_count = StudentFeeTransaction.query.filter_by(is_approved=False).count()
        fee_groups_count = ProgrammeFeeStructure.query.count()
        
        return render_template(
            'admin/finance_admin_dashboard.html',
            total_revenue=summary['total_revenue'],
            outstanding_balance=summary['outstanding_balance'],
            pending_count=summary['pending_count'],
            approved_count=summary['approved_count'],
            students_with_debt=summary['students_with_debt'],
            total_students=summary['total_students'],
            collection_rate=summary['collection_rate'],
            payments=payments,
            fee_structures=fees,
            balances=balances,
            fee_groups_count=fee_groups_count
        )
    
    except Exception as e:
        logger.exception(f"Error loading finance dashboard: {e}")
        return render_template(
            'admin/finance_admin_dashboard.html',
            total_revenue=0,
            outstanding_balance=0,
            pending_count=0,
            approved_count=0,
            students_with_debt=0,
            total_students=0,
            collection_rate=0,
            payments=[],
            fee_structures=[],
            balances=[],
            fee_groups_count=0
        )


@finance_bp.route('/payments', methods=['GET'])
@login_required
@require_finance_admin
def view_payments():
    """View all payments"""
    
    try:
        status_filter = request.args.get('status', '')
        query = StudentFeeTransaction.query
        
        if status_filter in ['pending', 'approved', 'rejected']:
            query = query.filter_by(status=status_filter)
        
        payments = query.order_by(StudentFeeTransaction.created_at.desc()).all()
        
        return render_template('admin/finance_payments.html', payments=payments)
    
    except Exception as e:
        logger.exception(f"Error viewing payments: {e}")
        abort(500)


@finance_bp.route('/approve/<int:payment_id>', methods=['POST'])
@login_required
@require_finance_admin
def approve_payment(payment_id):
    """Approve a pending payment"""
    
    try:
        payment = StudentFeeTransaction.query.get_or_404(payment_id)
        
        if payment.status != 'pending':
            return jsonify({'error': 'Payment is not pending'}), 400
        
        payment.status = 'approved'
        payment.approved_at = datetime.utcnow()
        payment.approved_by = current_user.admin_id
        db.session.commit()
        
        logger.info(f"Payment {payment_id} approved by {current_user.admin_id}")
        
        return jsonify({'success': True, 'message': 'Payment approved'}), 200
    
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error approving payment: {e}")
        return jsonify({'error': str(e)}), 500


@finance_bp.route('/reject/<int:payment_id>', methods=['POST'])
@login_required
@require_finance_admin
def reject_payment(payment_id):
    """Reject a pending payment"""
    
    try:
        payment = StudentFeeTransaction.query.get_or_404(payment_id)
        
        if payment.status != 'pending':
            return jsonify({'error': 'Payment is not pending'}), 400
        
        reason = request.json.get('reason', 'No reason provided')
        
        payment.status = 'rejected'
        payment.rejected_at = datetime.utcnow()
        payment.rejected_by = current_user.admin_id
        payment.rejection_reason = reason
        db.session.commit()
        
        logger.info(f"Payment {payment_id} rejected by {current_user.admin_id}")
        
        return jsonify({'success': True, 'message': 'Payment rejected'}), 200
    
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error rejecting payment: {e}")
        return jsonify({'error': str(e)}), 500


@finance_bp.route('/record-payment', methods=['POST'])
@login_required
@require_finance_admin
def record_payment():
    """Record a manual payment"""
    
    try:
        data = request.get_json()
        
        amount = float(data.get('amount', 0))
        method = data.get('method')
        reference = data.get('reference', '')
        notes = data.get('notes', '')
        student_id = data.get('student_id')
        
        if not amount or amount <= 0:
            return jsonify({'error': 'Invalid amount'}), 400
        
        if not method:
            return jsonify({'error': 'Payment method required'}), 400
        
        # Create payment record
        payment = StudentFeeTransaction(
            student_id=student_id,
            amount=amount,
            payment_method=method,
            reference_number=reference,
            notes=notes,
            status='approved',  # Manual payments are auto-approved
            approved_at=datetime.utcnow(),
            approved_by=current_user.admin_id
        )
        
        db.session.add(payment)
        
        # Update fee balance if exists
        balance = StudentFeeBalance.query.filter_by(student_id=student_id).first()
        if balance:
            balance.amount_paid += amount
            balance.balance = max(0, balance.amount_due - balance.amount_paid)
            balance.status = 'paid' if balance.balance <= 0 else 'partial'
        
        db.session.commit()
        
        logger.info(f"Payment recorded by {current_user.admin_id}: Student {student_id}, Amount: {amount}")
        
        return jsonify({'success': True, 'message': 'Payment recorded'}), 200
    
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error recording payment: {e}")
        return jsonify({'error': str(e)}), 500


@finance_bp.route('/fee-structures', methods=['GET'])
@login_required
@require_finance_admin
def view_fee_structures():
    """View all fee structures"""
    
    try:
        structures = ProgrammeFeeStructure.query.all()
        return render_template('admin/finance_fee_structures.html', structures=structures)
    
    except Exception as e:
        logger.exception(f"Error viewing fee structures: {e}")
        abort(500)


@finance_bp.route('/balances', methods=['GET'])
@login_required
@require_finance_admin
def view_balances():
    """View student fee balances"""
    
    try:
        logger.info("Fetching student balances...")
        balances = get_student_balances(limit=500)
        logger.info(f"Got {len(balances)} balances to display")
        
        # If no balances, log information about database state
        if not balances:
            balance_count = db.session.query(StudentFeeBalance).count()
            user_count = db.session.query(User).count()
            logger.warning(f"No balances returned. DB has {balance_count} StudentFeeBalance records and {user_count} User records")
        
        return render_template('admin/finance_balances.html', balances=balances)
    
    except Exception as e:
        logger.exception(f"Error viewing balances: {e}")
        abort(500)


@finance_bp.route('/reports', methods=['GET'])
@login_required
@require_finance_admin
def reports():
    """Financial reports with daily, weekly, monthly views"""
    
    try:
        # Get various report data
        daily_revenue = get_daily_revenue(days=30)
        payment_methods = get_payment_method_breakdown()
        top_debtors = get_top_debtors(limit=10)
        financial_summary = get_financial_summary()
        
        return render_template(
            'admin/finance_reports.html',
            daily_revenue=daily_revenue,
            payment_methods=payment_methods,
            top_debtors=top_debtors,
            financial_summary=financial_summary,
            now=datetime.utcnow()
        )
    
    except Exception as e:
        logger.exception(f"Error loading reports: {e}")
        abort(500)


# ============================================================
# API ENDPOINTS FOR REPORT DATA
# ============================================================

@finance_bp.route('/api/reports/daily', methods=['GET'])
@login_required
@require_finance_admin
def api_daily_report():
    """API endpoint for daily report data"""
    try:
        days = request.args.get('days', 30, type=int)
        daily_revenue = get_daily_revenue(days=days)
        
        # Add today's summary
        today = datetime.utcnow().date()
        today_data = next((d for d in daily_revenue if d['date'] == today.strftime('%Y-%m-%d')), None)
        
        today_transactions = get_today_transactions_count()
        pending_approvals = db.session.query(db.func.count(StudentFeeTransaction.id)).filter_by(is_approved=False).scalar() or 0
        
        return jsonify({
            'success': True,
            'data': daily_revenue,
            'today_revenue': today_data['total'] if today_data else 0,
            'today_transactions': today_transactions,
            'pending_approvals': pending_approvals,
            'total': sum(d['total'] for d in daily_revenue),
            'average': sum(d['total'] for d in daily_revenue) / len(daily_revenue) if daily_revenue else 0
        })
    except Exception as e:
        logger.exception(f"Error getting daily report: {e}")
        return jsonify({'success': False, 'message': str(e)}), 400


@finance_bp.route('/api/reports/weekly', methods=['GET'])
@login_required
@require_finance_admin
def api_weekly_report():
    """API endpoint for weekly report data"""
    try:
        weekly_data = get_weekly_revenue()
        current_week_total = sum(w['total'] for w in weekly_data['current_week'])
        last_week_total = sum(w['total'] for w in weekly_data['last_week'])
        
        trend = 0
        if last_week_total > 0:
            trend = ((current_week_total - last_week_total) / last_week_total) * 100
        
        return jsonify({
            'success': True,
            'current_week': weekly_data['current_week'],
            'last_week': weekly_data['last_week'],
            'current_week_total': current_week_total,
            'last_week_total': last_week_total,
            'trend': round(trend, 2),
            'best_day': max(weekly_data['current_week'], key=lambda x: x['total'])['day']
        })
    except Exception as e:
        logger.exception(f"Error getting weekly report: {e}")
        return jsonify({'success': False, 'message': str(e)}), 400


@finance_bp.route('/api/reports/monthly', methods=['GET'])
@login_required
@require_finance_admin
def api_monthly_report():
    """API endpoint for monthly report data"""
    try:
        monthly_data = get_monthly_revenue(months=12)
        current_month = datetime.utcnow().strftime('%B %Y')
        current_month_data = next((m for m in monthly_data if m['month'] == current_month), None)
        ytd_total = sum(m['total'] for m in monthly_data)
        
        financial_summary = get_financial_summary()
        
        return jsonify({
            'success': True,
            'monthly_data': monthly_data,
            'current_month_total': current_month_data['total'] if current_month_data else 0,
            'ytd_total': ytd_total,
            'collection_rate': financial_summary['collection_rate'],
            'outstanding_balance': financial_summary['outstanding_balance'],
            'department_breakdown': get_department_breakdown()
        })
    except Exception as e:
        logger.exception(f"Error getting monthly report: {e}")
        return jsonify({'success': False, 'message': str(e)}), 400


@finance_bp.route('/api/reports/transactions', methods=['GET'])
@login_required
@require_finance_admin
def api_transactions():
    """API endpoint for transactions data"""
    try:
        period = request.args.get('period', 'today')  # today, week, month
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        
        query = db.session.query(
            StudentFeeTransaction.id,
            StudentFeeTransaction.amount,
            StudentFeeTransaction.description,
            StudentFeeTransaction.timestamp,
            StudentFeeTransaction.is_approved,
            User.first_name,
            User.last_name
        ).join(StudentFeeTransaction.student)
        
        # Filter by period
        if period == 'today':
            today = datetime.utcnow().date()
            query = query.filter(
                db.func.date(StudentFeeTransaction.timestamp) == today
            )
        elif period == 'week':
            week_ago = datetime.utcnow() - timedelta(days=7)
            query = query.filter(StudentFeeTransaction.timestamp >= week_ago)
        elif period == 'month':
            month_ago = datetime.utcnow() - timedelta(days=30)
            query = query.filter(StudentFeeTransaction.timestamp >= month_ago)
        
        paginated = query.order_by(StudentFeeTransaction.timestamp.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        transactions = [
            {
                'id': t.id,
                'student_name': f"{t.first_name} {t.last_name}",
                'amount': float(t.amount),
                'method': t.description,
                'timestamp': t.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'Approved' if t.is_approved else 'Pending'
            }
            for t in paginated.items
        ]
        
        return jsonify({
            'success': True,
            'transactions': transactions,
            'total': paginated.total,
            'pages': paginated.pages,
            'current_page': page
        })
    except Exception as e:
        logger.exception(f"Error getting transactions: {e}")
        return jsonify({'success': False, 'message': str(e)}), 400


# ============================================================
# HELPER FUNCTIONS FOR REPORTS
# ============================================================

def get_daily_revenue(days=30):
    """Get daily revenue for last N days"""
    try:
        # Build start/end for the period (use dates to normalize)
        end_dt = datetime.utcnow()
        end_date = end_dt.date()
        start_date = end_date - timedelta(days=days - 1)

        # Use a database-compatible date expression for the report query.
        from sqlalchemy import func

        # Query sums grouped by date string (YYYY-MM-DD)
        rows = db.session.query(
            func.date(StudentFeeTransaction.timestamp).label('d'),
            db.func.sum(StudentFeeTransaction.amount).label('total')
        ).filter(
            StudentFeeTransaction.is_approved == True,
            StudentFeeTransaction.timestamp >= datetime.combine(start_date, datetime.min.time())
        ).group_by(func.date(StudentFeeTransaction.timestamp)).all()

        # Map results to dict by date string
        totals = { (r.d if isinstance(r.d, str) else r.d.strftime('%Y-%m-%d')): float(r.total or 0) for r in rows }

        # If no data in the recent window, fall back to aggregating across all approved transactions
        if not totals:
            rows_all = db.session.query(
                func.date(StudentFeeTransaction.timestamp).label('d'),
                db.func.sum(StudentFeeTransaction.amount).label('total')
            ).filter(
                StudentFeeTransaction.is_approved == True
            ).group_by(func.date(StudentFeeTransaction.timestamp)).order_by(func.date(StudentFeeTransaction.timestamp).desc()).limit(days).all()

            if rows_all:
                # Return the most recent 'days' with data, ordered ascending for chart labels
                recent = list(reversed(rows_all))
                return [
                    {
                        'date': (r.d if isinstance(r.d, str) else r.d.strftime('%Y-%m-%d')),
                        'total': float(r.total or 0)
                    }
                    for r in recent
                ]

        # Build list for each day in the range, ensuring zero for missing days
        out = []
        for i in range(days):
            day = start_date + timedelta(days=i)
            key = day.strftime('%Y-%m-%d')
            out.append({'date': key, 'total': float(totals.get(key, 0))})

        return out
    except Exception as e:
        logger.exception(f"Error calculating daily revenue: {e}")
        return []


def get_payment_method_breakdown():
    """Get payment breakdown by method"""
    try:
        from sqlalchemy import func
        
        breakdown = db.session.query(
            StudentFeeTransaction.description.label('method'),
            func.count(StudentFeeTransaction.id).label('count'),
            func.sum(StudentFeeTransaction.amount).label('total')
        ).filter(
            StudentFeeTransaction.is_approved == True
        ).group_by(
            StudentFeeTransaction.description
        ).all()
        
        return [
            {
                'method': b.method or 'Unknown',
                'count': b.count,
                'total': float(b.total or 0)
            }
            for b in breakdown
        ]
    except Exception as e:
        logger.exception(f"Error getting payment breakdown: {e}")
        return []


def get_top_debtors(limit=20):
    """Get students with highest debt"""
    try:
        from sqlalchemy import func
        
        # Calculate balance_remaining in the query
        balance_remaining = StudentFeeBalance.amount_due - func.coalesce(StudentFeeBalance.amount_paid, 0)
        
        debtors = db.session.query(
            User.first_name,
            User.middle_name,
            User.last_name,
            balance_remaining.label('balance')
        ).join(
            StudentFeeBalance, User.user_id == StudentFeeBalance.student_id
        ).filter(
            balance_remaining > 0
        ).order_by(
            balance_remaining.desc()
        ).limit(limit).all()
        
        return [
            {
                'name': ' '.join(filter(None, [d.first_name, d.middle_name, d.last_name])),
                'debt': float(d.balance or 0)
            }
            for d in debtors
        ]
    except Exception as e:
        logger.exception(f"Error getting top debtors: {e}")
        return []


def get_weekly_revenue():
    """Get weekly revenue breakdown"""
    try:
        from sqlalchemy import func
        
        now = datetime.utcnow()
        current_week_start = now - timedelta(days=now.weekday())
        last_week_start = current_week_start - timedelta(days=7)
        
        day_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        
        # Get current week data
        current_week = []
        for i in range(7):
            day = current_week_start + timedelta(days=i)
            day_date = day.date()
            
            revenue = db.session.query(
                db.func.sum(StudentFeeTransaction.amount)
            ).filter(
                StudentFeeTransaction.is_approved == True,
                db.func.date(StudentFeeTransaction.timestamp) == day_date
            ).scalar() or 0
            
            current_week.append({
                'day': day_labels[i],
                'date': day_date.strftime('%Y-%m-%d'),
                'total': float(revenue)
            })
        
        # Get last week data
        last_week = []
        for i in range(7):
            day = last_week_start + timedelta(days=i)
            day_date = day.date()
            
            revenue = db.session.query(
                db.func.sum(StudentFeeTransaction.amount)
            ).filter(
                StudentFeeTransaction.is_approved == True,
                db.func.date(StudentFeeTransaction.timestamp) == day_date
            ).scalar() or 0
            
            last_week.append({
                'day': day_labels[i],
                'date': day_date.strftime('%Y-%m-%d'),
                'total': float(revenue)
            })
        
        return {
            'current_week': current_week,
            'last_week': last_week
        }
    except Exception as e:
        logger.exception(f"Error calculating weekly revenue: {e}")
        return {'current_week': [], 'last_week': []}


def get_monthly_revenue(months=12):
    """Get monthly revenue for last N months"""
    try:
        from sqlalchemy import func
        
        monthly_data = []
        now = datetime.utcnow()
        
        for i in range(months):
            # Go back i months
            month_start = now.replace(day=1) - timedelta(days=30 * i)
            # Get the first day of current month
            current_month = month_start.replace(day=1)
            # Get first day of next month (end of current)
            if current_month.month == 12:
                next_month = current_month.replace(year=current_month.year + 1, month=1)
            else:
                next_month = current_month.replace(month=current_month.month + 1)
            
            revenue = db.session.query(
                db.func.sum(StudentFeeTransaction.amount)
            ).filter(
                StudentFeeTransaction.is_approved == True,
                StudentFeeTransaction.timestamp >= current_month,
                StudentFeeTransaction.timestamp < next_month
            ).scalar() or 0
            
            month_label = current_month.strftime('%B %Y')
            monthly_data.append({
                'month': month_label,
                'total': float(revenue),
                'date': current_month.strftime('%Y-%m')
            })
        
        return list(reversed(monthly_data))
    except Exception as e:
        logger.exception(f"Error calculating monthly revenue: {e}")
        return []


def get_today_transactions_count():
    """Get count of transactions today"""
    try:
        today = datetime.utcnow().date()
        count = db.session.query(db.func.count(StudentFeeTransaction.id)).filter(
            StudentFeeTransaction.is_approved == True,
            db.func.date(StudentFeeTransaction.timestamp) == today
        ).scalar() or 0
        return count
    except Exception as e:
        logger.exception(f"Error counting today's transactions: {e}")
        return 0


def get_department_breakdown():
    """Get revenue breakdown by department/category"""
    try:
        from sqlalchemy import func
        
        # Group by course program or fee type (customize based on your needs)
        breakdown = db.session.query(
            StudentProfile.current_programme.label('department'),
            func.count(StudentFeeTransaction.id).label('count'),
            func.sum(StudentFeeTransaction.amount).label('total')
        ).join(
            StudentProfile, StudentFeeTransaction.student_id == StudentProfile.user_id
        ).filter(
            StudentFeeTransaction.is_approved == True
        ).group_by(
            StudentProfile.current_programme
        ).all()
        
        result = []
        for b in breakdown:
            if b.department:  # Only include if department exists
                result.append({
                    'name': b.department,
                    'count': b.count,
                    'total': float(b.total or 0)
                })
        
        return result[:10]  # Return top 10
    except Exception as e:
        logger.exception(f"Error getting department breakdown: {e}")
        return []


# ============================================================
# REGISTER BLUEPRINT
# ============================================================
# In your main app.py, add:
# from finance_routes import finance_bp
# app.register_blueprint(finance_bp, url_prefix='/admin/finance')

