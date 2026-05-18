import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { signupUser, signinUser, generateRandomEmail } from '../helpers.js';

export const options = {
  scenarios: {
    signin_only: LOAD_SCENARIO,
  },
  thresholds: PERFORMANCE_THRESHOLDS,
};

// Guarantee a valid test user exists in the DB once before starting VUs
export function setup() {
  const email = generateRandomEmail();
  const password = 'K6_modular_password_2026';
  
  // Register the user once dynamically so that the database has a matching hash record
  signupUser(email, password);
  
  return { email, password };
}

export default function (data) {
  // Hits the Signin API directly, checking bcrypt CPU verification speeds
  signinUser(data.email, data.password);
  
  sleep(1);
}
