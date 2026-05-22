import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { signupUser, signinUser, listDocuments, generateRandomEmail } from '../helpers.js';

export const options = {
  scenarios: {
    list_documents_only: LOAD_SCENARIO,
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

  // 2. Fetch list of documents
  listDocuments(token);

  sleep(1);
}
