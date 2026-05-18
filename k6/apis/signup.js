import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { signupUser, generateRandomEmail } from '../helpers.js';

export const options = {
  scenarios: {
    signup_only: LOAD_SCENARIO,
  },
  thresholds: PERFORMANCE_THRESHOLDS,
};

export default function () {
  const email = generateRandomEmail();
  const password = 'K6_modular_password_2026';

  // Hits the Signup API directly
  signupUser(email, password);

  // Think time
  sleep(1);
}
