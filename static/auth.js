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
        if (result.access_token) localStorage.setItem('access_token', result.access_token);
        if (result.account_id) localStorage.setItem('account_id', result.account_id);
        say(location.pathname === '/register' ? 'Check your email to confirm your account.' : 'Signed in — redirecting…');
        if (location.pathname === '/login' && result.access_token) location.href = '/app?view=terminal';
    } catch (error) {
        say('Unable to connect to the authentication service.', true);
    }
});