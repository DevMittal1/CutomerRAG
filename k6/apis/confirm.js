import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { 
  signupUser, 
  signinUser, 
  getPresignedUrl, 
  confirmUpload, 
  generateRandomEmail 
} from '../helpers.js';

export const options = {
  scenarios: {
    confirm_only: LOAD_SCENARIO,
  },
  thresholds: PERFORMANCE_THRESHOLDS,
};

// Generate valid session token and a pre-registered pending document ID once to isolate writes
export function setup() {
  const email = generateRandomEmail();
  const password = 'K6_modular_password_2026';
  
  signupUser(email, password);
  const signinRes = signinUser(email, password);
  const token = signinRes.json('access_token');
  
  const presignedRes = getPresignedUrl(token, 'k6_isolated_confirm_doc.pdf', 'application/pdf', 1048576);
  const documentId = presignedRes.json('document_id');
  
  return { token, documentId };
}

export default function (data) {
  // Hits the confirm API directly, validating user permissions and atomic updates
  confirmUpload(data.token, data.documentId);
  
  sleep(1);
}
