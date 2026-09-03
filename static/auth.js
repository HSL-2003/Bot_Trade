(function () {
    'use strict';

    var form = document.querySelector('#auth-form');
    var msg = document.querySelector('#message');
    var path = window.location.pathname;
    var page = path === '/register' ? 'register'
        : (path === '/forgot-password' || path.indexOf('/forgot-password') === 0) ? 'forgot'
            : 'login';

    function say(t, error) {
        if (!msg) return;
        msg.textContent = t;
        msg.className = 'message ' + (error ? 'error' : '');
    }

    // Forgot-password flow is email-only: the template hides the password group
    // pre-paint (html.no-password); remove it here so browser validation never
    // blocks the submit on a hidden, required input.
    if (page === 'forgot') {
        var pwGroup = document.querySelector('#password-group');
        if (pwGroup) pwGroup.remove();
    }

    // Login page: surface the password-recovery entry point next to the switch link.
    if (page === 'login') {
        var sw = document.querySelector('.switch');
        if (sw && !sw.querySelector('a[href="/forgot-password"]')) {
            var link = document.createElement('a');
            link.href = '/forgot-password';
            link.textContent = 'Forgot password?';
            sw.appendChild(document.createTextNode(' · '));
            sw.appendChild(link);
        }
    }

    // Backend detail can be a string (HTTPException) or an array of validation
    // errors (FastAPI 422) — normalise both into a readable single message.
    function extractDetail(payload, fallback) {
        var detail = payload && payload.detail;
        if (typeof detail === 'string' && detail.trim()) return detail;
        if (Array.isArray(detail)) {
            var parts = detail.map(function (item) {
                var field = Array.isArray(item.loc)
                    ? item.loc.filter(function (p) { return p !== 'body'; }).join('.')
                    : '';
                return field ? (field + ': ' + (item.msg || '')).trim() : (item.msg || '');
            }).filter(Boolean);
            if (parts.length) return parts.join('; ');
        }
        if (payload && typeof payload.message === 'string' && payload.message.trim()) {
            return payload.message;
        }
        return fallback;
    }

    function postJSON(endpoint, body) {
        return fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function (response) {
            // Some proxies return non-JSON error pages — never assume JSON.
            return response.json().catch(function () { return null; }).then(function (payload) {
                return { ok: response.ok, status: response.status, payload: payload };
            });
        });
    }

    function completeSignIn() {
        say('Authentication verified — warping to terminal…');
        if (typeof window.triggerLoginTunnelTransition === 'function') {
            window.triggerLoginTunnelTransition('/app?view=terminal');
        } else {
            setTimeout(function () {
                window.location.href = '/app?view=terminal';
            }, 150);
        }
    }

    if (form) {
        form.addEventListener('submit', async function (e) {
            e.preventDefault();
            var emailInput = document.querySelector('#email');
            var passwordInput = document.querySelector('#password');
            var email = emailInput ? emailInput.value.trim().toLowerCase() : '';
            var submitBtn = form.querySelector('.submit-btn');
            if (submitBtn) submitBtn.disabled = true;

            try {
                if (page === 'forgot') {
                    if (!email) return say('Enter your email address first.', true);
                    // No dedicated reset endpoint exists yet; the passwordless
                    // magic link doubles as the recovery flow.
                    var reset = await postJSON('/api/auth/magic-link', { email: email });
                    if (!reset.ok) {
                        return say(extractDetail(reset.payload, 'Unable to send a reset link.'), true);
                    }
                    return say((reset.payload && reset.payload.message)
                        || 'Check your email for a secure sign-in link.');
                }

                var endpoint = page === 'register' ? '/api/auth/register' : '/api/auth/login';
                say(page === 'register' ? 'Creating your workspace…' : 'Verifying credentials…');
                var result = await postJSON(endpoint, {
                    email: email,
                    password: passwordInput ? passwordInput.value : undefined
                });

                if (!result.ok) {
                    var fallback = result.status === 429
                        ? 'Too many attempts — please wait a minute and try again.'
                        : 'Authentication failed.';
                    return say(extractDetail(result.payload, fallback), true);
                }

                if (result.payload && result.payload.access_token) {
                    localStorage.setItem('access_token', result.payload.access_token);
                    if (result.payload.account_id) {
                        localStorage.setItem('account_id', result.payload.account_id);
                    }
                    completeSignIn();
                    return;
                }
                say(page === 'register' ? 'Check your email to confirm your account.' : 'Signed in.');
            } catch (error) {
                say('Unable to connect to the authentication service.', true);
            } finally {
                if (submitBtn) submitBtn.disabled = false;
            }
        });
    }

    // Magic link (passwordless sign-in) — wired to the template button.
    async function continueWithEmail() {
        var btn = document.querySelector('#magic-link-btn');
        var emailInput = document.querySelector('#email');
        var email = emailInput ? emailInput.value.trim().toLowerCase() : '';
        if (!email) return say('Enter your email address first.', true);
        if (btn) btn.disabled = true;
        try {
            var result = await postJSON('/api/auth/magic-link', { email: email });
            if (!result.ok) {
                return say(extractDetail(result.payload, 'Unable to send a sign-in link.'), true);
            }
            say((result.payload && result.payload.message)
                || 'Check your email for a secure sign-in link.');
        } catch (error) {
            say('Unable to reach the authentication service.', true);
        } finally {
            if (btn) btn.disabled = false;
        }
    }
    window.continueWithEmail = continueWithEmail;
    var magicBtn = document.querySelector('#magic-link-btn');
    if (magicBtn) magicBtn.addEventListener('click', continueWithEmail);

    // Handle Social Logins (Google & GitHub)
    (function initSocialAuth() {
        var googleBtn = document.querySelector('#google-auth-btn');
        var githubBtn = document.querySelector('#github-auth-btn');

        async function handleOAuth(provider, e) {
            if (window.__SUPABASE_CONFIG__ && window.__SUPABASE_CONFIG__.url && window.__SUPABASE_CONFIG__.anonKey && window.supabase) {
                try {
                    e.preventDefault();
                    say('Connecting to ' + (provider === 'google' ? 'Google' : 'GitHub') + '…');
                    var sb = window.supabase.createClient(window.__SUPABASE_CONFIG__.url, window.__SUPABASE_CONFIG__.anonKey);
                    var result = await sb.auth.signInWithOAuth({
                        provider: provider,
                        options: {
                            redirectTo: window.location.origin + '/auth/callback'
                        }
                    });
                    if (result && result.error) {
                        say(result.error.message || ('Unable to start ' + provider + ' login.'), true);
                        setTimeout(function () {
                            window.location.href = '/api/auth/' + provider;
                        }, 500);
                    }
                } catch (err) {
                    window.location.href = '/api/auth/' + provider;
                }
            }
        }

        if (googleBtn) googleBtn.addEventListener('click', function (e) { handleOAuth('google', e); });
        if (githubBtn) githubBtn.addEventListener('click', function (e) { handleOAuth('github', e); });
    })();
})();