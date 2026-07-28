from .common import health_check, csrf_token, public_slot_config, public_lab_catalog
from .auth import (
    patient_register, patient_login, patient_login_otp, patient_logout,
    get_current_patient, patient_reset_password, staff_login, staff_logout, auth_me,
)
from .otp import otp_send, otp_verify
from .patient_lookup import validate_old_patient, lookup_patient
from .booking import available_slots, book_token, cancel_token, cancel_token_public
from .reception import (
    search_patient, check_in_patient, register_walkin_patient,
    reception_appointments, reception_lab_payments, pay_lab_fee, pay_lab_fees_for_token, throttle_status,
    reception_tokens_booked, reception_patients, reception_patient_detail,
)
from .doctor import (
    doctor_schedule, doctor_queue, next_patient,
    start_consultation, complete_consultation, patient_history,
    doctor_completed_today, doctor_consultation_detail, doctor_patient_record,
)
from .lab import lab_queue, lab_start_test, lab_complete_test, lab_reports_for_token
from .pharmacy import pharmacy_queue, pharmacy_start_dispense, pharmacy_mark_ready_view, pharmacy_complete_dispense
from .admin_api import (
    admin_doctors, admin_add_doctor, admin_update_doctor,
    admin_staff_list, admin_staff_detail,
    admin_slot_config, admin_throttle_config, admin_throttle_logs, analytics,
)
from .patient_portal import (
    get_patient_tokens, patient_queue_status, patient_prescriptions,
    patient_lab_reports, patient_lab_payments, patient_pay_lab_fee, patient_pay_lab_fees_for_token,
    patient_bills, create_followup, patient_followups,
    patient_followup_booking_context, patient_book_followup,
)
from .patient_journey import patient_journey
from .sync import system_sync
from .queue import waiting_queue
