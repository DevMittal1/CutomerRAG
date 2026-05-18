import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { signupUser, signinUser, getPresignedUrl, generateRandomEmail } from '../helpers.js';

export const options = {
  scenarios: {
    presigned_url_only: LOAD_SCENARIO,
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
  const filename = `k6_isolated_doc_${__VU}_${__ITER}.pdf`;
  
  // Hits the S3 signature generation API directly
  getPresignedUrl(data.token, filename, 'application/pdf', 1048576); // 1MB
  
  sleep(1);
}
