import { sleep } from 'k6';
import { BREAKPOINT_SCENARIO } from './config.js';
import { 
  generateRandomEmail, 
  signupUser, 
  signinUser, 
  getUserProfile, 
  getPresignedUrl, 
  confirmUpload 
} from './helpers.js';

// Options for Breakpoint Testing:
// We let the test scale up fully without prematurely crashing the suite on thresholds,
// so that we can identify the true breakpoint metrics.
export const options = {
  scenarios: {
    breakpoint: BREAKPOINT_SCENARIO,
  },
};

/**
 * Breakpoint Virtual User Loop
 * Ramps up traffic aggressively to find the maximum concurrent capability
 */
export default function () {
  const email = generateRandomEmail();
  const password = 'K6_breakpoint_password_2026';

  // 1. Signup
  const signupRes = signupUser(email, password);
  if (signupRes.status !== 201) {
    sleep(0.5);
    return;
  }

  // 2. Signin
  const signinRes = signinUser(email, password);
  if (signinRes.status !== 200) {
    sleep(0.5);
    return;
  }
  const token = signinRes.json('access_token');

  // 3. User Profile Lookup
  getUserProfile(token);

  // 4. Request S3 Pre-signed URL
  const filename = `breakpoint_doc_${__VU}_${__ITER}.pdf`;
  const presignedRes = getPresignedUrl(token, filename, 'application/pdf', 2097152); // 2MB
  if (presignedRes.status === 200) {
    const documentId = presignedRes.json('document_id');
    
    // Simulate upload delay
    sleep(0.1); 
    
    // 5. Confirm S3 Upload has finished
    confirmUpload(token, documentId);
  }

  sleep(0.5);
}
