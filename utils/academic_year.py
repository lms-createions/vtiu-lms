"""Helpers for the single admin-configured academic year."""


def configured_academic_year():
    """Return the configured academic year as a four-digit start year."""
    from models import AcademicYear

    period = AcademicYear.query.order_by(AcademicYear.start_date.desc()).first()
    return str(period.start_date.year) if period and period.start_date else None


def configured_academic_year_or_none(value=None):
    """Return the configured year, ignoring user-supplied year text."""
    return configured_academic_year()
