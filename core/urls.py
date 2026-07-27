from django.urls import path
from . import views

urlpatterns = [
    path('health/', views.health_check, name='health'),
    path('csrf/', views.csrf_token, name='csrf'),
    path('slot-config/', views.public_slot_config, name='slot-config'),
    path('lab-tests/', views.public_lab_catalog, name='lab-tests'),

    # Auth
    path('auth/staff/login/', views.staff_login, name='staff-login'),
    path('auth/staff/logout/', views.staff_logout, name='staff-logout'),
    path('auth/me/', views.auth_me, name='auth-me'),
    path('otp/send/', views.otp_send, name='otp-send'),
    path('otp/verify/', views.otp_verify, name='otp-verify'),
    path('patient/register/', views.patient_register, name='patient-register'),
    path('patient/login/', views.patient_login, name='patient-login'),
    path('patient/login/otp/', views.patient_login_otp, name='patient-login-otp'),
    path('patient/logout/', views.patient_logout, name='patient-logout'),
    path('patient/me/', views.get_current_patient, name='patient-me'),
    path('patient/reset-password/', views.patient_reset_password, name='patient-reset-password'),
    path('patient/validate/', views.validate_old_patient, name='patient-validate'),
    path('patient/lookup/', views.lookup_patient, name='patient-lookup'),

    # Booking
    path('slots/', views.available_slots, name='slots'),
    path('book/', views.book_token, name='book'),
    path('cancel/<int:token_id>/', views.cancel_token_public, name='cancel'),

    # Reception
    path('search/', views.search_patient, name='search'),
    path('check-in/<int:token_id>/', views.check_in_patient, name='check-in'),
    path('reception/register/', views.register_walkin_patient, name='reception-register'),
    path('reception/appointments/', views.reception_appointments, name='reception-appointments'),
    path('reception/tokens-booked/', views.reception_tokens_booked, name='reception-tokens-booked'),
    path('reception/patients/', views.reception_patients, name='reception-patients'),
    path('reception/patients/<int:user_id>/', views.reception_patient_detail, name='reception-patient-detail'),
    path('reception/lab-payments/', views.reception_lab_payments, name='reception-lab-payments'),
    path('reception/lab-pay/<int:order_id>/', views.pay_lab_fee, name='reception-lab-pay'),
    path('reception/lab-pay-token/<int:token_id>/', views.pay_lab_fees_for_token, name='reception-lab-pay-token'),
    path('reception/throttle/', views.throttle_status, name='reception-throttle'),

    # Queue
    path('waiting-queue/', views.waiting_queue, name='waiting-queue'),
    path('waiting-queue/<int:doctor_id>/', views.waiting_queue, name='waiting-queue-doctor'),

    # Doctor
    path('doctor/schedule/', views.doctor_schedule, name='doctor-schedule'),
    path('doctor-queue/', views.doctor_queue, name='doctor-queue'),
    path('doctor-queue/<int:doctor_id>/', views.doctor_queue, name='doctor-queue-id'),
    path('next-patient/', views.next_patient, name='next-patient'),
    path('next-patient/<int:doctor_id>/', views.next_patient, name='next-patient-id'),
    path('start-consult/<int:token_id>/', views.start_consultation, name='start-consult'),
    path('complete-consult/<int:token_id>/', views.complete_consultation, name='complete-consult'),
    path('doctor/completed-today/', views.doctor_completed_today, name='doctor-completed-today'),
    path('doctor/consultation/<int:token_id>/', views.doctor_consultation_detail, name='doctor-consultation-detail'),
    path('patient-history/<int:token_id>/', views.patient_history, name='patient-history'),
    path('doctor/patient-record/', views.doctor_patient_record, name='doctor-patient-record'),

    # Lab
    path('lab/queue/', views.lab_queue, name='lab-queue'),
    path('lab/orders/<int:order_id>/start/', views.lab_start_test, name='lab-start'),
    path('lab/orders/<int:order_id>/complete/', views.lab_complete_test, name='lab-complete'),
    path('lab/reports/<int:token_id>/', views.lab_reports_for_token, name='lab-reports-token'),

    # Pharmacy
    path('pharmacy/queue/', views.pharmacy_queue, name='pharmacy-queue'),
    path('pharmacy/<int:entry_id>/start/', views.pharmacy_start_dispense, name='pharmacy-start'),
    path('pharmacy/<int:entry_id>/ready/', views.pharmacy_mark_ready_view, name='pharmacy-ready'),
    path('pharmacy/<int:entry_id>/complete/', views.pharmacy_complete_dispense, name='pharmacy-complete'),

    # Patient portal
    path('patient/tokens/', views.get_patient_tokens, name='patient-tokens'),
    path('patient/journey/', views.patient_journey, name='patient-journey'),
    path('patient/queue-status/', views.patient_queue_status, name='patient-queue-status'),
    path('patient/prescriptions/', views.patient_prescriptions, name='patient-prescriptions'),
    path('patient/lab-reports/', views.patient_lab_reports, name='patient-lab-reports'),
    path('patient/lab-payments/', views.patient_lab_payments, name='patient-lab-payments'),
    path('patient/lab-pay/<int:order_id>/', views.patient_pay_lab_fee, name='patient-lab-pay'),
    path('patient/lab-pay-token/<int:token_id>/', views.patient_pay_lab_fees_for_token, name='patient-lab-pay-token'),
    path('patient/followups/', views.patient_followups, name='patient-followups'),
    path('patient/bills/', views.patient_bills, name='patient-bills'),
    path('followup/<int:token_id>/', views.create_followup, name='followup'),

    # Admin
    path('admin/staff/', views.admin_staff_list, name='admin-staff'),
    path('admin/staff/<int:user_id>/', views.admin_staff_detail, name='admin-staff-detail'),
    path('admin/doctors/', views.admin_doctors, name='admin-doctors'),
    path('admin/doctors/add/', views.admin_add_doctor, name='admin-add-doctor'),
    path('admin/doctors/<int:doctor_id>/', views.admin_update_doctor, name='admin-update-doctor'),
    path('admin/slots/config/', views.admin_slot_config, name='admin-slot-config'),
    path('admin/throttle/config/', views.admin_throttle_config, name='admin-throttle-config'),
    path('admin/throttle/logs/', views.admin_throttle_logs, name='admin-throttle-logs'),
    path('analytics/', views.analytics, name='analytics'),
    path('sync/', views.system_sync, name='system-sync'),
]
