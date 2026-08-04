// General OPD System - Core Utilities

function medIconSvg(name, size = 18) {
    return (typeof MedIcons !== 'undefined') ? MedIcons.svg(name, size) : '';
}

function statusIconSvg(statusKey, size = 14) {
    return (typeof MedIcons !== 'undefined') ? MedIcons.status(statusKey, size) : '';
}

// Toast Notification
class Toast {
    static container = null;
    static init() { if (!this.container) { this.container = document.createElement('div'); this.container.className = 'toast-container'; document.body.appendChild(this.container); } }
    static show(message, type = 'info') {
        this.init();
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        const icons = { success: 'check', error: 'cross', warning: 'warning', info: 'info' };
        const iconHtml = (typeof MedIcons !== 'undefined')
            ? MedIcons.svg(icons[type] || 'info', 18)
            : '';
        toast.innerHTML = `<span class="toast-icon">${iconHtml}</span><span>${message}</span>`;
        this.container.appendChild(toast);
        setTimeout(() => { toast.style.animation = 'slideOut 0.3s ease'; setTimeout(() => toast.remove(), 300); }, 3000);
    }
}

// Loading Overlay
class LoadingOverlay {
    static show() { const overlay = document.createElement('div'); overlay.id = 'loading-overlay'; overlay.className = 'loading-overlay'; overlay.innerHTML = '<div class="spinner"></div>'; document.body.appendChild(overlay); }
    static hide() { document.getElementById('loading-overlay')?.remove(); }
}

// Storage Manager
const StorageManager = {
    set(key, value) { sessionStorage.setItem(`smartOPD_${key}`, JSON.stringify(value)); },
    get(key) { const data = sessionStorage.getItem(`smartOPD_${key}`); return data ? JSON.parse(data) : null; },
    remove(key) { sessionStorage.removeItem(`smartOPD_${key}`); },
    getUser() {
        if (typeof API !== 'undefined' && API.getStoredUser) {
            return API.getStoredUser() || this.get('user');
        }
        return this.get('user');
    },
    setUser(user) {
        if (typeof API !== 'undefined' && API.setAuth && API.getToken()) {
            API.setAuth(API.getToken(), { ...user, role: user.role || API.getStoredUser()?.role });
        }
        this.set('user', user);
    },
    logout(redirectUrl = '../index.html') {
        if (typeof API !== 'undefined' && API.logout) {
            API.logout(redirectUrl);
            return;
        }
        this.remove('user');
        window.location.href = redirectUrl;
    },
    getBookings() { return this.get('bookings') || []; },
    addBooking(booking) { const bookings = this.getBookings(); bookings.push(booking); this.set('bookings', bookings); },
    getLabReports() { return this.get('labReports') || []; },
    addLabReport(report) { const reports = this.getLabReports(); reports.push(report); this.set('labReports', reports); },
    getBills() { return this.get('bills') || []; },
    addBill(bill) { const bills = this.getBills(); bills.push(bill); this.set('bills', bills); }
};

// Time Utilities — shared slot status logic (matches backend ConsultationSlot times)
const TimeUtils = {
    SLOT_CONFIG: {
        MORNING: {
            startHour: 9, startMinute: 0, endHour: 11, endMinute: 0,
            startTime: '09:00', endTime: '11:00',
            displayTime: '9:00 AM - 11:00 AM',
            checkinOpensMinutesBefore: 15,
        },
        AFTERNOON: {
            startHour: 12, startMinute: 0, endHour: 14, endMinute: 0,
            startTime: '12:00', endTime: '14:00',
            displayTime: '12:00 PM - 2:00 PM',
            checkinOpensMinutesBefore: 15,
        },
        EVENING: {
            startHour: 15, startMinute: 0, endHour: 17, endMinute: 0,
            startTime: '15:00', endTime: '17:00',
            displayTime: '3:00 PM - 5:00 PM',
            checkinOpensMinutesBefore: 15,
        },
    },

    getCurrentTime() { return new Date(); },

    formatTime(date) {
        return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
    },

    formatDate(date) {
        return date.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
    },

    normalizeSlotType(slotType) {
        if (!slotType) return null;
        const raw = String(slotType).trim().toUpperCase();
        if (raw.startsWith('MORNING') || raw === 'M') return 'MORNING';
        if (raw.startsWith('AFTERNOON') || raw === 'A') return 'AFTERNOON';
        if (raw.startsWith('EVENING') || raw === 'E') return 'EVENING';
        return raw;
    },

    parseDate(dateInput) {
        if (!dateInput) return null;
        if (dateInput instanceof Date) return new Date(dateInput.getTime());
        const parsed = new Date(`${String(dateInput).slice(0, 10)}T00:00:00`);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    },

    /** Local calendar date as YYYY-MM-DD (never use toISOString for this). */
    localDateISO(d = new Date()) {
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
    },

    addLocalDays(isoOrDate, days) {
        const base = this.parseDate(isoOrDate) || new Date();
        const d = new Date(base.getTime());
        d.setDate(d.getDate() + days);
        return this.localDateISO(d);
    },

    formatApptDateLabel(isoDate, now = new Date()) {
        if (!isoDate) return '—';
        const relation = this.compareAppointmentDay(isoDate, now);
        if (relation === 'today') return 'Today';
        if (relation === 'future' && this.isSameLocalDay(isoDate, this.addLocalDays(now, 1))) return 'Tomorrow';
        const d = this.parseDate(isoDate);
        if (!d) return isoDate;
        return d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
    },

    isSameLocalDay(a, b) {
        const left = this.parseDate(a);
        const right = this.parseDate(b);
        if (!left || !right) return false;
        return left.getFullYear() === right.getFullYear()
            && left.getMonth() === right.getMonth()
            && left.getDate() === right.getDate();
    },

    parseClockToMinutes(timeStr) {
        if (!timeStr) return null;
        const value = String(timeStr).trim();
        const match24 = value.match(/^(\d{1,2}):(\d{2})(?::\d{2})?$/);
        if (match24) return parseInt(match24[1], 10) * 60 + parseInt(match24[2], 10);
        const match12 = value.match(/^(\d{1,2}):(\d{2})\s*(AM|PM)$/i);
        if (match12) {
            let hour = parseInt(match12[1], 10);
            const minute = parseInt(match12[2], 10);
            const meridiem = match12[3].toUpperCase();
            if (meridiem === 'PM' && hour !== 12) hour += 12;
            if (meridiem === 'AM' && hour === 12) hour = 0;
            return hour * 60 + minute;
        }
        return null;
    },

    /** Sort A1, A2, … A10 (not A1, A10, A2). */
    compareTokenNumbers(a, b) {
        const parse = (token) => {
            const m = String(token || '').match(/^([A-Za-z]+)(\d+)$/);
            if (!m) return [String(token || ''), 0];
            return [m[1].toUpperCase(), parseInt(m[2], 10)];
        };
        const [prefixA, numA] = parse(a);
        const [prefixB, numB] = parse(b);
        if (prefixA !== prefixB) return prefixA.localeCompare(prefixB);
        return numA - numB;
    },

    getSlotBounds(appointment) {
        const slotType = this.normalizeSlotType(
            appointment.slot || appointment.slot_type || appointment.slot_type_raw
        );
        const config = slotType ? this.SLOT_CONFIG[slotType] : null;
        const startMinutes = this.parseClockToMinutes(appointment.start_time)
            ?? (config ? config.startHour * 60 + config.startMinute : null);
        const endMinutes = this.parseClockToMinutes(appointment.end_time)
            ?? (config ? config.endHour * 60 + config.endMinute : null);
        const checkinOpensBefore = config?.checkinOpensMinutesBefore ?? 15;
        const checkinOpenMinutes = startMinutes != null
            ? Math.max(0, startMinutes - checkinOpensBefore)
            : null;
        return {
            slotType,
            startMinutes,
            endMinutes,
            checkinOpenMinutes,
            displayTime: config?.displayTime || '',
        };
    },

    nowMinutes(now = new Date()) {
        return now.getHours() * 60 + now.getMinutes();
    },

    compareAppointmentDay(appointmentDate, now = new Date()) {
        const aptDate = this.parseDate(appointmentDate);
        if (!aptDate) return 'unknown';
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const day = new Date(aptDate.getFullYear(), aptDate.getMonth(), aptDate.getDate());
        if (day.getTime() > today.getTime()) return 'future';
        if (day.getTime() < today.getTime()) return 'past';
        return 'today';
    },

    getCurrentSlot(now = new Date()) {
        const current = this.nowMinutes(now);
        for (const [slotType, config] of Object.entries(this.SLOT_CONFIG)) {
            const start = config.startHour * 60 + config.startMinute;
            const end = config.endHour * 60 + config.endMinute;
            if (current >= start && current < end) return slotType;
        }
        return null;
    },

    getAppointmentSlotStatus(appointment, now = new Date()) {
        const tokenStatus = String(appointment.status || '').toLowerCase().replace(/-/g, '_');
        const bounds = this.getSlotBounds(appointment);
        const dayRelation = this.compareAppointmentDay(appointment.date, now);
        const currentMinutes = this.nowMinutes(now);

        // Prefer real visit/workflow status over slot-time "Passed".
        // Past-day open visits are completed — do not keep "With Doctor" forever.
        if (dayRelation === 'past' && ['checked_in', 'consulting', 'pending_lab', 'pending_pharmacy'].includes(tokenStatus)) {
            return {
                status: 'completed', label: 'Completed', class: 'status-completed', icon: '✓',
                isPassed: false, isActive: false, isUpcoming: false, canCheckIn: false,
            };
        }
        if (tokenStatus === 'completed') {
            return {
                status: 'completed', label: 'Completed', class: 'status-completed', icon: '✓',
                isPassed: false, isActive: false, isUpcoming: false, canCheckIn: false,
            };
        }
        if (tokenStatus === 'checked_in') {
            return {
                status: 'in_queue', label: 'Waiting', class: 'status-ongoing', icon: '⏳',
                isPassed: false, isActive: true, isUpcoming: false, canCheckIn: false,
            };
        }
        if (tokenStatus === 'consulting') {
            return {
                status: 'consulting', label: 'With Doctor', class: 'status-ongoing', icon: '🩺',
                isPassed: false, isActive: true, isUpcoming: false, canCheckIn: false,
            };
        }
        if (tokenStatus === 'pending_lab') {
            return {
                status: 'pending_lab', label: 'Lab', class: 'status-ongoing', icon: '🔬',
                isPassed: false, isActive: true, isUpcoming: false, canCheckIn: false,
            };
        }
        if (tokenStatus === 'pending_pharmacy') {
            return {
                status: 'pending_pharmacy', label: 'Pharmacy', class: 'status-ongoing', icon: '💊',
                isPassed: false, isActive: true, isUpcoming: false, canCheckIn: false,
            };
        }
        if (tokenStatus === 'cancelled') {
            return {
                status: 'cancelled', label: 'Cancelled', class: 'status-expired', icon: '✗',
                isPassed: true, isActive: false, isUpcoming: false, canCheckIn: false,
            };
        }
        if (tokenStatus === 'expired') {
            return {
                status: 'passed', label: 'No-show', class: 'status-expired', icon: '⛔',
                isPassed: true, isActive: false, isUpcoming: false, canCheckIn: false,
            };
        }

        // Time-based labels only for booked (not yet checked in) appointments.
        if (dayRelation === 'future') {
            return {
                status: 'upcoming', label: 'Upcoming', class: 'status-upcoming', icon: '📅',
                isPassed: false, isActive: false, isUpcoming: true, canCheckIn: false,
            };
        }
        if (dayRelation === 'past') {
            return {
                status: 'passed', label: 'No-show', class: 'status-expired', icon: '⛔',
                isPassed: true, isActive: false, isUpcoming: false, canCheckIn: false,
            };
        }

        if (bounds.startMinutes == null || bounds.endMinutes == null) {
            const passedByDay = dayRelation === 'past';
            return {
                status: passedByDay ? 'passed' : 'unknown',
                label: passedByDay ? 'No-show' : 'Unknown',
                class: passedByDay ? 'status-expired' : '',
                icon: passedByDay ? '⛔' : '?',
                isPassed: passedByDay,
                isActive: false,
                isUpcoming: false,
                canCheckIn: false,
            };
        }

        if (currentMinutes < bounds.startMinutes) {
            const canCheckIn = currentMinutes >= bounds.checkinOpenMinutes
                && (tokenStatus === 'booked' || tokenStatus === 'pending');
            return {
                status: 'upcoming', label: 'Upcoming', class: 'status-upcoming', icon: '📅',
                isPassed: false, isActive: false, isUpcoming: true, canCheckIn,
            };
        }
        if (currentMinutes >= bounds.startMinutes && currentMinutes <= bounds.endMinutes) {
            const canCheckIn = tokenStatus === 'booked' || tokenStatus === 'pending';
            return {
                status: 'active', label: 'Active', class: 'status-ongoing', icon: '🔄',
                isPassed: false, isActive: true, isUpcoming: false, canCheckIn,
            };
        }
        return {
            status: 'passed', label: 'No-show', class: 'status-expired', icon: '⛔',
            isPassed: true, isActive: false, isUpcoming: false, canCheckIn: false,
        };
    },

    isSlotPassed(slotType, date = null, now = new Date()) {
        const appointmentDate = date || now;
        return this.getAppointmentSlotStatus(
            { slot: slotType, date: appointmentDate, status: 'booked' },
            now
        ).isPassed;
    },

    isSlotActive(slotType, date = null, now = new Date()) {
        const appointmentDate = date || now;
        return this.getAppointmentSlotStatus(
            { slot: slotType, date: appointmentDate, status: 'booked' },
            now
        ).isActive;
    },

    isSlotUpcoming(slotType, date = null, now = new Date()) {
        const appointmentDate = date || now;
        return this.getAppointmentSlotStatus(
            { slot: slotType, date: appointmentDate, status: 'booked' },
            now
        ).isUpcoming;
    },

    canCheckIn(appointment, now = new Date()) {
        return this.getAppointmentSlotStatus(appointment, now).canCheckIn;
    },

    isWithinCheckInWindow(slotType, checkInTime, date = null) {
        const appointmentDate = date || checkInTime;
        const bounds = this.getSlotBounds({ slot: slotType, date: appointmentDate });
        if (bounds.checkinOpenMinutes == null || bounds.endMinutes == null) return false;
        const arrival = this.nowMinutes(checkInTime);
        return arrival >= bounds.checkinOpenMinutes && arrival <= bounds.endMinutes;
    },

    formatMinutesAsTime(totalMinutes) {
        const hour = Math.floor(totalMinutes / 60);
        const minute = totalMinutes % 60;
        const date = new Date(2000, 0, 1, hour, minute);
        return date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    },

    getCheckInUnavailableReason(appointment, now = new Date()) {
        const slotStatus = this.getAppointmentSlotStatus(appointment, now);
        if (slotStatus.canCheckIn) return null;

        const bounds = this.getSlotBounds(appointment);
        const dayRelation = this.compareAppointmentDay(appointment.date, now);
        const openTime = bounds.checkinOpenMinutes != null
            ? this.formatMinutesAsTime(bounds.checkinOpenMinutes)
            : 'the slot start';

        if (dayRelation === 'future') {
            const aptDate = this.parseDate(appointment.date);
            const dateLabel = aptDate ? this.formatDate(aptDate) : 'the appointment day';
            return `Check-in opens on ${dateLabel} at ${openTime} (15 minutes before the slot).`;
        }
        if (dayRelation === 'past' || slotStatus.isPassed) {
            return 'This appointment was marked as a no-show. Check-in is no longer available.';
        }
        if (slotStatus.isUpcoming) {
            return `Check-in opens today at ${openTime} (15 minutes before the slot).`;
        }
        return 'Check-in is not available right now.';
    },

    applyServerSlotConfig(slots = {}) {
        Object.entries(slots).forEach(([slotType, cfg]) => {
            const type = this.normalizeSlotType(slotType);
            if (!type || !this.SLOT_CONFIG[type] || !cfg) return;
            const [startHour, startMinute] = String(cfg.start_time || '09:00').split(':').map(Number);
            const [endHour, endMinute] = String(cfg.end_time || '11:00').split(':').map(Number);
            Object.assign(this.SLOT_CONFIG[type], {
                startHour,
                startMinute,
                endHour,
                endMinute,
                startTime: cfg.start_time,
                endTime: cfg.end_time,
                displayTime: cfg.time_range || this.SLOT_CONFIG[type].displayTime,
                checkinOpensMinutesBefore: cfg.checkin_opens_minutes_before ?? 15,
                avgConsultationMinutes: cfg.avg_consultation_minutes,
                maxTokens: cfg.max_tokens,
            });
        });
    },

    async loadSlotConfig() {
        try {
            const res = await fetch('/api/core/slot-config/', { credentials: 'include' });
            const data = await res.json();
            if (data.success && data.slots) {
                this.applyServerSlotConfig(data.slots);
            }
        } catch (_) {
            /* keep defaults */
        }
    },
};

const PatientPriority = {
    ELDERLY_AGE: 70,

    isElderlyByAge(age) {
        const value = parseInt(age, 10);
        return !Number.isNaN(value) && value >= this.ELDERLY_AGE;
    },

    elderlyCheckboxHtml(age, checkboxId) {
        const isElderly = this.isElderlyByAge(age);
        if (isElderly) {
            return `<label><input type="checkbox" id="${checkboxId}" checked disabled> Elderly (70+ — automatic)</label>`;
        }
        return `<label style="opacity:0.65"><input type="checkbox" id="${checkboxId}" disabled> Elderly (70+ only — not applicable)</label>`;
    },

    resolveElderlyFromAge(age) {
        return this.isElderlyByAge(age);
    },

    syncModalElderlyCheckbox(ageInputId, checkboxId) {
        const ageInput = document.getElementById(ageInputId);
        const checkbox = document.getElementById(checkboxId);
        if (!ageInput || !checkbox) return;
        const isElderly = this.isElderlyByAge(ageInput.value);
        checkbox.checked = isElderly;
        checkbox.disabled = true;
    },

    defaultDisabledFlag(appointment) {
        if (!appointment) return false;
        return !!(appointment.is_disabled || appointment.patient_is_disabled);
    },

    categoryLabel(patient) {
        if (!patient) return 'General';
        if (patient.is_elderly) return 'Elderly';
        if (patient.is_disabled) return 'Disabled';
        const cat = String(patient.category || '').toUpperCase();
        if (cat === 'ELDERLY') return 'Elderly';
        if (cat === 'DISABLED') return 'Disabled';
        if (cat && cat !== 'GENERAL') return patient.category;
        return 'General';
    },

    isPriorityPatient(patient) {
        if (!patient) return false;
        return !!(patient.is_elderly || patient.is_disabled
            || ['ELDERLY', 'DISABLED'].includes(String(patient.category || '').toUpperCase()));
    },
};

const PatientDisplay = {
    label(name, patientId) {
        const n = String(name || '').trim() || 'Patient';
        return patientId ? `${n} (${patientId})` : n;
    },
};

/** Normalize medicine lines for every digital prescription view (doctor / pharmacy / patient). */
const PrescriptionFormat = {
    normalizeLine(medicine) {
        const m = medicine || {};
        const name = m.medicine_name || m.name || '—';
        const dosage = m.dosage || '—';
        const frequency = m.frequency || '—';
        let duration = '—';
        if (m.duration_days != null && m.duration_days !== '') {
            const days = Number(m.duration_days);
            duration = Number.isFinite(days) ? `${days} day${days === 1 ? '' : 's'}` : String(m.duration_days);
        } else if (m.duration) {
            duration = m.duration;
        }
        const instructions = m.instructions || '—';
        return { name, dosage, frequency, duration, instructions };
    },

    lineSummary(medicine) {
        const m = this.normalizeLine(medicine);
        return `${m.name} — ${m.dosage}, ${m.frequency}, ${m.duration}, ${m.instructions}`;
    },

    tableHeadHtml() {
        return '<tr><th>Medicine</th><th>Dosage</th><th>Frequency</th><th>Duration</th><th>Instructions</th></tr>';
    },

    tableBodyHtml(medicines, escapeHtml = (s) => String(s ?? '')) {
        const rows = (medicines || []).map((raw) => {
            const m = this.normalizeLine(raw);
            return `<tr><td>${escapeHtml(m.name)}</td><td>${escapeHtml(m.dosage)}</td><td>${escapeHtml(m.frequency)}</td><td>${escapeHtml(m.duration)}</td><td>${escapeHtml(m.instructions)}</td></tr>`;
        });
        if (!rows.length) {
            return '<tr><td colspan="5" style="text-align:center;color:#999;">No medicines prescribed</td></tr>';
        }
        return rows.join('');
    },

    tableHtml(medicines, escapeHtml) {
        return `<table class="medicine-table"><thead>${this.tableHeadHtml()}</thead><tbody>${this.tableBodyHtml(medicines, escapeHtml)}</tbody></table>`;
    },
};

// Export
window.Toast = Toast;
window.LoadingOverlay = LoadingOverlay;
window.StorageManager = StorageManager;
window.TimeUtils = TimeUtils;
window.PatientPriority = PatientPriority;
window.PatientDisplay = PatientDisplay;
window.PrescriptionFormat = PrescriptionFormat;

function logoutPortal(redirectUrl = '../index.html') {
    if (typeof API !== 'undefined' && API.logout) {
        API.logout(redirectUrl);
    } else {
        window.location.href = redirectUrl;
    }
}
window.logoutPortal = logoutPortal;

/** Unified appointment status labels — matches backend DISPLAY_STATUS */
const AppointmentStatus = {
    LABELS: {
        booked: 'Booked',
        checked_in: 'Waiting',
        consulting: 'With Doctor',
        pending_lab: 'Lab',
        pending_pharmacy: 'Pharmacy',
        completed: 'Completed',
        expired: 'No-show',
        cancelled: 'Cancelled',
    },
    CSS: {
        booked: 'status-upcoming',
        checked_in: 'status-ongoing',
        consulting: 'status-ongoing',
        pending_lab: 'status-ongoing',
        pending_pharmacy: 'status-ongoing',
        completed: 'status-completed',
        expired: 'status-expired',
        cancelled: 'status-expired',
    },
    normalize(status) {
        return String(status || '').toLowerCase().replace(/-/g, '_');
    },
    label(status) {
        const key = this.normalize(status);
        return this.LABELS[key] || (status ? String(status).replace(/_/g, ' ') : 'Unknown');
    },
    badgeClass(status) {
        return this.CSS[this.normalize(status)] || '';
    },
    isActive(status) {
        return ['booked', 'checked_in', 'consulting', 'pending_lab', 'pending_pharmacy'].includes(this.normalize(status));
    },
    isTerminal(status) {
        return ['completed', 'expired', 'cancelled'].includes(this.normalize(status));
    },
};

/** Highlight the active sidebar item (staff + patient dashboards). */
const SidebarNav = {
    activate(navEl) {
        if (!navEl || !navEl.classList.contains('sidebar-nav-item')) return;
        const scope = navEl.closest('nav') || navEl.closest('aside') || document;
        scope.querySelectorAll('.sidebar-nav-item').forEach((item) => {
            item.classList.remove('active');
        });
        navEl.classList.add('active');
    },

    setActiveByKey(key, root = document) {
        if (!key) return;
        const el = root.querySelector(`.sidebar-nav-item[data-nav="${key}"]`);
        if (el) this.activate(el);
    },

    highlightCurrentPage() {
        const page = window.location.pathname.split('/').pop() || 'dashboard.html';
        const el = document.querySelector(`.sidebar-nav-item[data-page="${page}"]`);
        if (el) this.activate(el);
    },
};

/** Shared polling — triggers slot expiry via /sync/ then runs callback */
const PollManager = {
    _timers: {},
    start(key, callback, intervalMs = 5000) {
        this.stop(key);
        const tick = async () => {
            try { await API.sync(); } catch (_) { /* non-fatal */ }
            await callback();
        };
        tick();
        this._timers[key] = setInterval(tick, intervalMs);
    },
    stop(key) {
        if (this._timers[key]) {
            clearInterval(this._timers[key]);
            delete this._timers[key];
        }
    },
};

window.AppointmentStatus = AppointmentStatus;
window.PollManager = PollManager;
window.SidebarNav = SidebarNav;
TimeUtils.loadSlotConfig();