import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { signupUser, signinUser, getUserProfile, generateRandomEmail } from '../helpers.js';

export const options = {
  scenarios: {
    me_only: LOAD_SCENARIO,
  },
  thresholds: PERFORMANCE_THRESHOLDS,
};

// Generate valid session token once to isolate the GET /me query path
export function setup() {
  const email = generateRandomEmail();
  const password = 'K6_modular_password_2026';
  
  signupUser(email, password);
  const signinRes = signinUser(email, password);
  
  return { token: signinRes.json('access_token') };
}

export default function (data) {
  // Queries profile directly using pre-arranged token
  getUserProfile(data.token);
  
  sleep(1);
}
