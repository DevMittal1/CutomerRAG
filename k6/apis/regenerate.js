import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { signupUser, signinUser, getPresignedUrl, regenerateUploadUrl, generateRandomEmail } from '../helpers.js';

export const options = {
  scenarios: {
    regenerate_url_only: LOAD_SCENARIO,
  },
  thresholds: PERFORMANCE_THRESHOLDS,
};

export default function () {
  const email = generateRandomEmail();
  const password = 'K6_modular_password_2026';

  // 1. Setup user & session
  signupUser(email, password);
  const loginRes = signinUser(email, password);
  const token = loginRes.json('access_token');

  // 2. Register document to get document_id
  const docRes = getPresignedUrl(token, 'test_doc.pdf', 'application/pdf', 2048);
  const docId = docRes.json('document_id');

  // 3. Regenerate the presigned upload URL
  regenerateUploadUrl(token, docId);

  sleep(1);
}
