import { sleep } from 'k6';
import { 
  PERFORMANCE_THRESHOLDS, 
  LOAD_SCENARIO 
} from './config.js';
import { 
  generateRandomEmail, 
  signupUser, 
  signinUser, 
  getUserProfile, 
  getPresignedUrl, 
  listDocuments,
  regenerateUploadUrl,
  confirmUpload,
  streamChat
} from './helpers.js';

// Define the scaling stages and rigid SLA thresholds
export const options = {
  scenarios: {
    peak_load: LOAD_SCENARIO,
  },
  thresholds: PERFORMANCE_THRESHOLDS,
};

/**
 * The Virtual User Loop Flow
 * Simulates a real user signup, login, profile fetch, file upload request, and confirmation
 */
export default function () {
  const email = generateRandomEmail();
  const password = 'K6_strong_password_2026';

  // 1. Signup a unique user
  const signupRes = signupUser(email, password);
  if (signupRes.status !== 201) {
    sleep(1);
    return;
  }

  // 2. Signin to retrieve JWT Token
  const signinRes = signinUser(email, password);
  if (signinRes.status !== 200) {
    sleep(1);
    return;
  }
  const token = signinRes.json('access_token');

  // 3. Retrieve user profile (validates authorization & header bindings)
  getUserProfile(token);

  // 4. Request a pre-signed PUT S3 URL
  const filename = `loadtest_document_${__VU}_${__ITER}.pdf`;
  const presignedRes = getPresignedUrl(token, filename, 'application/pdf', 1572864); // 1.5MB mock PDF
  if (presignedRes.status === 200) {
    const documentId = presignedRes.json('document_id');
    
    // 4.1. List documents to confirm registration
    listDocuments(token);

    // 4.2. Regenerate S3 upload URL (simulates retry/regeneration)
    regenerateUploadUrl(token, documentId);

    // Simulates physical network S3 upload delay (e.g. 200ms)
    sleep(0.2); 
    
    // 5. Confirm S3 Upload has finished and persist metadata
    confirmUpload(token, documentId);

    // 6. Conversational chat query scoping search against the document
    const messages = [
      { role: 'user', content: 'What is the compliance status of this document?' }
    ];
    streamChat(token, messages, [documentId]);
  }

  // Simulate human "thinking" time between interactions
  sleep(1);
}
