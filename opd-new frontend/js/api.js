// General OPD — Django API client (per-tab Bearer token + CSRF)

const API = {
    base: '/api/core',
    AUTH_TOKEN_KEY: 'opd_api_token',
    AUTH_USER_KEY: 'opd_api_user',

    getCookie(name) {
        const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
        return match ? decodeURIComponent(match[2]) : null;
    },

    getToken() {
        return sessionStorage.getItem(this.AUTH_TOKEN_KEY);
    },

    getStoredUser() {
        try {
            const raw = sessionStorage.getItem(this.AUTH_USER_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch {
            return null;
        }
    },

    setAuth(token, user) {
        if (token) sessionStorage.setItem(this.AUTH_TOKEN_KEY, token);
        if (user) sessionStorage.setItem(this.AUTH_USER_KEY, JSON.stringify(user));
    },

    clearAuth() {
        sessionStorage.removeItem(this.AUTH_TOKEN_KEY);
        sessionStorage.removeItem(this.AUTH_USER_KEY);
    },

    async ensureCsrf() {
        if (this.getCookie('csrftoken')) return;
        await fetch(`${this.base}/csrf/`, { credentials: 'include' });
    },

    async request(method, path, body = null, isForm = false, options = {}) {
        await this.ensureCsrf();
        const headers = { 'X-CSRFToken': this.getCookie('csrftoken') || '' };
        const skipAuth = !!options.skipAuth;
        const token = skipAuth ? null : this.getToken();
        if (token) headers.Authorization = `Bearer ${token}`;
        const opts = { method, credentials: 'include', headers };
        if (body !== null) {
            if (isForm) {
                opts.body = body;
            } else {
                headers['Content-Type'] = 'application/json';
                opts.body = JSON.stringify(body);
            }
        }
        const res = await fetch(`${this.base}${path}`, opts);
        const contentType = res.headers.get('content-type') || '';
        let data;
        try {
            if (contentType.includes('application/json')) {
                data = await res.json();
            } else {
                const text = await res.text();
                data = {
                    success: false,
                    error: res.status >= 500
                        ? `Server error (${res.status}). Please refresh or contact support.`
                        : `Invalid server response (${res.status})`,
                    _raw: text.slice(0, 200),
                };
            }
        } catch {
            data = { success: false, error: `Network error (${res.status || 'unknown'})` };
        }
        if (!data.error && data.detail) {
            data.error = Array.isArray(data.detail)
                ? data.detail.map((d) => (d.msg || d)).join('; ')
                : String(data.detail);
        }
        if (!res.ok && !data.error) data.error = `Request failed (${res.status})`;
        if (data.success === undefined && res.ok) data.success = true;
        if (data.success === undefined && !res.ok) data.success = false;
        return data;
    },

    get(path, options) { return this.request('GET', path, null, false, options); },
    post(path, body, options) { return this.request('POST', path, body, false, options); },
    put(path, body, options) { return this.request('PUT', path, body, false, options); },

    maybeEsewaRedirect(res) {
        if (res && res.success && res.form_url && res.form_fields) {
            this.submitEsewaRedirect(res.form_url, res.form_fields);
            return true;
        }
        return false;
    },

    // Auth
    patientRegister(d) { return this.post('/patient/register/', d, { skipAuth: true }); },
    patientLogin(d) {
        this.clearAuth();
        return this.post('/patient/login/', d, { skipAuth: true }).then((data) => {
            if (data.success && data.token) {
                this.setAuth(data.token, { ...data.patient, role: 'patient' });
            }
            return data;
        });
    },
    patientLoginOtp(d) {
        this.clearAuth();
        return this.post('/patient/login/otp/', d, { skipAuth: true }).then((data) => {
            if (data.success && data.token) {
                this.setAuth(data.token, { ...data.patient, role: 'patient' });
            }
            return data;
        });
    },
    patientLogout() { return this.post('/patient/logout/', {}); },
    patientMe() { return this.get('/patient/me/'); },
    patientResetPassword(d) { return this.post('/patient/reset-password/', d); },
    otpSend(d) { return this.post('/otp/send/', d); },
    otpVerify(d) { return this.post('/otp/verify/', d); },
    validateOldPatient(d) { return this.post('/patient/validate/', d); },
    lookupPatient(patientId, phone) {
        let q = `patient_id=${encodeURIComponent(patientId)}`;
        if (phone) q += `&phone=${encodeURIComponent(phone)}`;
        return this.get(`/patient/lookup/?${q}`);
    },
    staffLogin(d) {
        this.clearAuth();
        return this.post('/auth/staff/login/', d, { skipAuth: true }).then((data) => {
            if (data.success && data.token) this.setAuth(data.token, data.user);
            return data;
        });
    },
    staffLogout() { return this.post('/auth/staff/logout/', {}); },
    authMe() { return this.get('/auth/me/'); },

    async logout(redirectUrl = '../index.html') {
        const user = this.getStoredUser();
        try {
            if (user?.role === 'patient') {
                await this.patientLogout();
            } else if (user) {
                await this.staffLogout();
            }
        } catch (_) { /* clear local session even if revoke fails */ }
        this.clearAuth();
        if (redirectUrl) window.location.href = redirectUrl;
    },

    // Booking
    getSlots(date) { return this.get(`/slots/${date ? '?date=' + date : ''}`); },
    getSlotConfig() { return this.get('/slot-config/'); },
    labCatalog() { return this.get('/lab-tests/'); },
    bookToken(d) {
        return this.post('/book/', d).then((res) => {
            this.maybeEsewaRedirect(res);
            return res;
        });
    },
    cancelToken(id) { return this.post(`/cancel/${id}/`, {}); },
    esewaInitiateBooking(d) { return this.post('/payments/esewa/booking/', d); },
    esewaInitiateFollowup(d) { return this.post('/payments/esewa/followup/', d); },
    esewaInitiateLabOrder(orderId) { return this.post(`/payments/esewa/lab-order/${orderId}/`, {}); },
    esewaInitiateLabToken(tokenId) { return this.post(`/payments/esewa/lab-token/${tokenId}/`, {}); },
    submitEsewaRedirect(formUrl, fields) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = formUrl;
        Object.entries(fields || {}).forEach(([key, value]) => {
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = key;
            input.value = value == null ? '' : String(value);
            form.appendChild(input);
        });
        document.body.appendChild(form);
        form.submit();
    },

    // Patient portal
    patientTokens() { return this.get('/patient/tokens/'); },
    patientJourney() { return this.get('/patient/journey/'); },
    patientQueueStatus() { return this.get('/patient/queue-status/'); },
    patientPrescriptions() { return this.get('/patient/prescriptions/'); },
    patientLabReports() { return this.get('/patient/lab-reports/'); },
    patientLabPayments() { return this.get('/patient/lab-payments/'); },
    patientPayLabFee(orderId, d) { return this.post(`/patient/lab-pay/${orderId}/`, d || {}); },
    patientPayLabFeesForToken(tokenId, d) { return this.post(`/patient/lab-pay-token/${tokenId}/`, d || {}); },
    patientFollowups() { return this.get('/patient/followups/'); },
    createFollowup(tokenId, d) { return this.post(`/followup/${tokenId}/`, d || {}); },
    patientFollowupBookingContext(tokenId) { return this.get(`/patient/followup-booking/${tokenId}/`); },
    patientBookFollowup(d) { return this.post('/patient/followup-book/', d || {}); },
    patientBills() { return this.get('/patient/bills/'); },

    // Reception
    searchPatient(q, scope = 'checkin') {
        const params = new URLSearchParams({ q });
        if (scope) params.set('scope', scope);
        return this.get(`/search/?${params.toString()}`);
    },
    checkIn(tokenId, d) { return this.post(`/check-in/${tokenId}/`, d || {}); },
    receptionRegister(d) { return this.post('/reception/register/', d); },
    receptionAppointments(opts = {}) {
        const params = new URLSearchParams();
        if (opts.view) params.set('view', opts.view);
        if (opts.day) params.set('day', opts.day);
        const q = params.toString() ? `?${params.toString()}` : '';
        return this.get(`/reception/appointments/${q}`);
    },
    receptionTokensBooked() { return this.get('/reception/tokens-booked/'); },
    receptionPatients(q = '') {
        if (!q) return this.get('/reception/patients/');
        return this.get(`/reception/patients/?q=${encodeURIComponent(q)}`);
    },
    receptionUnbookedPatients() { return this.get('/reception/patients/unbooked-today/'); },
    receptionPatientDetail(userId) { return this.get(`/reception/patients/${userId}/`); },
    receptionUpdatePatient(userId, d) { return this.put(`/reception/patients/${userId}/`, d); },
    receptionLabPayments() { return this.get('/reception/lab-payments/'); },
    payLabFee(orderId, d) { return this.post(`/reception/lab-pay/${orderId}/`, d || {}); },
    payLabFeesForToken(tokenId, d) { return this.post(`/reception/lab-pay-token/${tokenId}/`, d || {}); },
    throttleStatus() { return this.get('/reception/throttle/'); },
    waitingQueue(doctorId) {
        return this.get(doctorId ? `/waiting-queue/${doctorId}/` : '/waiting-queue/');
    },

    // Doctor
    doctorSchedule() { return this.get('/doctor/schedule/'); },
    doctorQueue() { return this.get('/doctor-queue/'); },
    nextPatient() { return this.get('/next-patient/'); },
    startConsult(tokenId) { return this.post(`/start-consult/${tokenId}/`, {}); },
    completeConsult(tokenId, d) { return this.post(`/complete-consult/${tokenId}/`, d); },
    doctorPatientHistory(tokenId) { return this.get(`/patient-history/${tokenId}/`); },
    doctorPatientRecord(patientId) {
      return this.get(`/doctor/patient-record/?patient_id=${encodeURIComponent(patientId)}`);
    },
    doctorCompletedToday() { return this.get('/doctor/completed-today/'); },
    doctorConsultationDetail(tokenId) { return this.get(`/doctor/consultation/${tokenId}/`); },

    // Lab
    labQueue() { return this.get('/lab/queue/'); },
    labStart(orderId) { return this.post(`/lab/orders/${orderId}/start/`, {}); },
    labComplete(orderId, formData) { return this.request('POST', `/lab/orders/${orderId}/complete/`, formData, true); },

    // Pharmacy
    pharmacyQueue() { return this.get('/pharmacy/queue/'); },
    pharmacyStart(entryId) { return this.post(`/pharmacy/${entryId}/start/`, {}); },
    pharmacyReady(entryId) { return this.post(`/pharmacy/${entryId}/ready/`, {}); },
    pharmacyComplete(entryId, d) { return this.post(`/pharmacy/${entryId}/complete/`, d || {}); },

    // Admin & sync
    sync() { return this.get('/sync/'); },
    analytics(opts = {}) {
        const params = new URLSearchParams();
        if (opts.monthly_start) params.set('monthly_start', opts.monthly_start);
        if (opts.monthly_end) params.set('monthly_end', opts.monthly_end);
        const q = params.toString() ? `?${params.toString()}` : '';
        return this.get(`/analytics/${q}`);
    },
    adminDoctors() { return this.get('/admin/doctors/'); },
    adminAddDoctor(d) { return this.post('/admin/doctors/add/', d); },
    adminUpdateDoctor(id, d) { return this.put(`/admin/doctors/${id}/`, d); },
    adminStaff(role) {
        const q = role ? `?role=${encodeURIComponent(role)}` : '';
        return this.get(`/admin/staff/${q}`);
    },
    adminStaffDetail(id) { return this.get(`/admin/staff/${id}/`); },
    adminCreateStaff(d) { return this.post('/admin/staff/', d); },
    adminUpdateStaff(id, d) { return this.put(`/admin/staff/${id}/`, d); },
    adminDeleteStaff(id) { return this.request('DELETE', `/admin/staff/${id}/`); },
    adminSlotConfig() { return this.get('/admin/slots/config/'); },
    adminSaveSlotConfig(d) { return this.put('/admin/slots/config/', d); },
    adminThrottleConfig() { return this.get('/admin/throttle/config/'); },
    adminSaveThrottleConfig(d) { return this.put('/admin/throttle/config/', d); },

    requireStaff(roles, redirectUrl = '../staff/login.html') {
        return this.authMe().then(data => {
            if (!data.success || !data.user || !roles.includes(data.user.role)) {
                window.location.href = redirectUrl;
                return null;
            }
            return data.user;
        });
    },

    requirePatient(redirectUrl = 'login.html') {
        return this.authMe().then(data => {
            if (!data.success || !data.user || data.user.role !== 'patient') {
                window.location.href = redirectUrl;
                return null;
            }
            return data.user;
        });
    },
};

window.API = API;
