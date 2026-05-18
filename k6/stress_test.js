import { sleep } from 'k6';
import http from 'k6/http';
import { check } from 'k6';
import { 
  BASE_URL,
  STRESS_SCENARIO 
} from './config.js';
import { 
  generateRandomEmail, 
  signupUser, 
  signinUser 
} from './helpers.js';

// Enforce custom SLAs for the stress test.
// We expect rate limiting (429s) to trigger, but we must have ZERO server crashes (500s).
export const options = {
  scenarios: {
    system_stress: STRESS_SCENARIO,
  },
  thresholds: {
    // We expect zero system crashes (HTTP 5xx status codes) under extreme stress
    'http_req_failed': ['rate<0.01'], 
    'http_req_duration': ['p(95)<300'],
  },
};

/**
 * Stress Test VU Flow
 * Rapidly requests profile details to purposely trigger sliding window rate limiter
 */
export default function () {
  const email = generateRandomEmail();
  const password = 'K6_stress_password_2026';

  // 1. Attempt signup
  const signupRes = signupUser(email, password);
  if (signupRes.status !== 201) {
    // If rate-limited, ensure it was a clean 429 and not a 500
    check(signupRes, {
      'crashed with 5xx': (res) => res.status < 500,
      'rate limited cleanly with 429': (res) => res.status === 429,
    });
    sleep(0.5);
    return;
  }

  // 2. Attempt login
  const signinRes = signinUser(email, password);
  if (signinRes.status !== 200) {
    check(signinRes, {
      'crashed with 5xx': (res) => res.status < 500,
      'rate limited cleanly with 429': (res) => res.status === 429,
    });
    sleep(0.5);
    return;
  }
  const token = signinRes.json('access_token');

  const params = {
    headers: {
      'Authorization': `Bearer ${token}`,
      'X-Request-ID': `k6-stress-profile-${__VU}-${__ITER}`,
    },
  };

  // 3. Repeatedly query profile without delay to hit sliding-window rate limit threshold
  for (let i = 0; i < 5; i++) {
    const profileRes = http.get(`${BASE_URL}/auth/me`, params);
    
    check(profileRes, {
      'profile call did not crash (not 5xx)': (res) => res.status < 500,
      'profile responded with 200 or 429': (res) => res.status === 200 || res.status === 429,
    });

    // Minimal sleep to maintain extreme spike pressure
    sleep(0.1); 
  }
}
