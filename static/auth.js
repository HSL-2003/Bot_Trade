const form = document.querySelector('#auth-form');
const msg = document.querySelector('#message');

function say(t, error = false) {
    msg.textContent = t;
    msg.className = 'message ' + (error ? 'error' : '');
}

form?.addEventListener('submit', async e => {
    e.preventDefault();
    try {
        const email = document.querySelector('#email').value.trim().toLowerCase();
        const password = document.querySelector('#password')?.value;
        const endpoint = location.pathname === '/register' ? '/api/auth/register' : '/api/auth/login';
        const response = await fetch(endpoint, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        const result = await response.json();
        if (!response.ok) return say(result.detail || 'Authentication failed.', true);
        if (result.access_token) {
            localStorage.setItem('access_token', result.access_token);
            if (result.account_id) localStorage.setItem('account_id', result.account_id);
            say('Authentication verified — warping to terminal…');
            if (typeof window.triggerLoginTunnelTransition === 'function') {
                window.triggerLoginTunnelTransition('/app?view=terminal');
            } else {
                setTimeout(() => {
                    location.href = '/app?view=terminal';
                }, 150);
            }
            return;
        }
        say(location.pathname.includes('register') ? 'Check your email to confirm your account.' : 'Signed in.');
    } catch (error) {
        say('Unable to connect to the authentication service.', true);
    }
});

async function continueWithEmail() {
    const btn = document.querySelector('#magic-link-btn');
    const email = document.querySelector('#email')?.value.trim().toLowerCase();
    if (!email) return say('Enter your email address first.', true);
    if (btn) btn.disabled = true;
    try {
        const response = await fetch('/api/auth/magic-link', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email })
        });
        const result = await response.json();
        if (!response.ok) return say(result.detail || 'Unable to send a sign-in link.', true);
        say('Check your email for a secure sign-in link.');
    } catch (error) {
        say('Unable to reach the authentication service.', true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// Handle Social Logins (Google & GitHub)
(function initSocialAuth() {
    const googleBtn = document.querySelector('#google-auth-btn');
    const githubBtn = document.querySelector('#github-auth-btn');

    async function handleOAuth(provider, e) {
        if (window.__SUPABASE_CONFIG__ && window.__SUPABASE_CONFIG__.url && window.__SUPABASE_CONFIG__.anonKey && window.supabase) {
            try {
                e.preventDefault();
                say(`Connecting to ${provider === 'google' ? 'Google' : 'GitHub'}…`);
                const sb = window.supabase.createClient(window.__SUPABASE_CONFIG__.url, window.__SUPABASE_CONFIG__.anonKey);
                const { error } = await sb.auth.signInWithOAuth({
                    provider: provider,
                    options: {
                        redirectTo: `${window.location.origin}/auth/callback`
                    }
                });
                if (error) {
                    say(error.message || `Unable to start ${provider} login.`, true);
                    setTimeout(() => {
                        window.location.href = `/api/auth/${provider}`;
                    }, 500);
                }
            } catch (err) {
                window.location.href = `/api/auth/${provider}`;
            }
        }
    }

    if (googleBtn) googleBtn.addEventListener('click', (e) => handleOAuth('google', e));
    if (githubBtn) githubBtn.addEventListener('click', (e) => handleOAuth('github', e));
})();