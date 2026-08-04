"""Canonical appointment lifecycle constants — single source of truth."""

# Token (appointment) statuses stored in DB
BOOKED = 'booked'
CHECKED_IN = 'checked_in'
CONSULTING = 'consulting'
PENDING_LAB = 'pending_lab'
PENDING_PHARMACY = 'pending_pharmacy'
COMPLETED = 'completed'
EXPIRED = 'expired'
CANCELLED = 'cancelled'

ACTIVE_STATUSES = (
    BOOKED,
    CHECKED_IN,
    CONSULTING,
    PENDING_LAB,
    PENDING_PHARMACY,
)

COMPLETED_STATUSES = (COMPLETED,)

TERMINAL_STATUSES = (COMPLETED, EXPIRED, CANCELLED)

# Statuses that prevent booking the same slot again (cancelled/expired may rebook)
DUPLICATE_BOOKING_BLOCK_STATUSES = ACTIVE_STATUSES + (COMPLETED,)

IN_QUEUE_STATUSES = (CHECKED_IN, CONSULTING)

# Unified display labels shown on every dashboard
DISPLAY_STATUS = {
    BOOKED: 'Booked',
    CHECKED_IN: 'Waiting',
    CONSULTING: 'With Doctor',
    PENDING_LAB: 'Lab',
    PENDING_PHARMACY: 'Pharmacy',
    COMPLETED: 'Completed',
    EXPIRED: 'No-show',
    CANCELLED: 'Cancelled',
}

# Pharmacy queue statuses
PHARMACY_WAITING = 'waiting'
PHARMACY_DISPENSING = 'dispensing'
PHARMACY_READY = 'ready'
PHARMACY_DONE = 'done'

PHARMACY_DISPLAY = {
    PHARMACY_WAITING: 'Prescription Received',
    PHARMACY_DISPENSING: 'Preparing',
    PHARMACY_READY: 'Ready for Pickup',
    PHARMACY_DONE: 'Dispensed',
}

ELDERLY_AGE_THRESHOLD = 70
# Fallback only when a test is not in the standard lab catalog.
DEFAULT_LAB_FEE = 500

# Auto-throttle pauses check-ins when doctor queue exceeds max_queue_size.
AUTO_THROTTLE_ENABLED = False

# Demo seeder phones — not counted for real-patient PAT serial (PAT0001, …).
DEMO_PATIENT_PHONE_PREFIXES = ('9800100', '9800110')
