import { sleep } from 'k6';
import { PERFORMANCE_THRESHOLDS, LOAD_SCENARIO } from '../config.js';
import { signupUser, signinUser, streamChat, generateRandomEmail } from '../helpers.js';

export const options = {
  scenarios: {
    chat_stream_only: LOAD_SCENARIO,
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

  // 2. Send grounded chat stream query
  const messages = [
    { role: 'user', content: 'What is the corporate compliance policy for data protection?' }
  ];
  streamChat(token, messages);

  sleep(1);
}
