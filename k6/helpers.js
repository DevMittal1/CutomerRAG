import http from 'k6/http';
import { check } from 'k6';
import { BASE_URL } from './config.js';

/**
 * Generates a unique, RFC-compliant email address for transient virtual users.
 */
export function generateRandomEmail() {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let randomStr = '';
  for (let i = 0; i < 8; i++) {
    randomStr += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return `k6_user_${randomStr}@loadtesting.com`;
}

/**
 * Registers a new virtual user.
 */
export function signupUser(email, password) {
  const payload = JSON.stringify({
    email: email,
    password: password,
  });

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'X-Request-ID': `k6-signup-${__VU}-${__ITER}`,
    },
  };

  const response = http.post(`${BASE_URL}/auth/signup`, payload, params);

  check(response, {
    'signup responded with 201': (res) => res.status === 201,
    'signup returned user ID': (res) => res.json('id') !== undefined,
  });

  return response;
}

/**
 * Authenticates a virtual user and retrieves a JWT access token.
 */
export function signinUser(email, password) {
  const payload = JSON.stringify({
    email: email,
    password: password,
  });

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'X-Request-ID': `k6-signin-${__VU}-${__ITER}`,
    },
  };

  const response = http.post(`${BASE_URL}/auth/signin`, payload, params);

  check(response, {
    'signin responded with 200': (res) => res.status === 200,
    'signin returned access token': (res) => res.json('access_token') !== undefined,
  });

  return response;
}

/**
 * Queries current active user profile information.
 */
export function getUserProfile(token) {
  const params = {
    headers: {
      'Authorization': `Bearer ${token}`,
      'X-Request-ID': `k6-profile-${__VU}-${__ITER}`,
    },
  };

  const response = http.get(`${BASE_URL}/auth/me`, params);

  check(response, {
    'profile responded with 200': (res) => res.status === 200,
    'profile email is correct': (res) => res.json('email') !== undefined,
    'rate limit headers injected': (res) => res.headers['X-Ratelimit-Remaining'] !== undefined,
  });

  return response;
}

/**
 * Requests an offline AWS Signature Version 4 pre-signed PUT URL.
 */
export function getPresignedUrl(token, filename, contentType, sizeBytes) {
  const payload = JSON.stringify({
    filename: filename,
    content_type: contentType,
    file_size_bytes: sizeBytes,
  });

  const params = {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Request-ID': `k6-presigned-${__VU}-${__ITER}`,
    },
  };

  const response = http.post(`${BASE_URL}/documents/presigned-url`, payload, params);

  check(response, {
    'presigned URL responded with 200': (res) => res.status === 200,
    'presigned URL contains file key': (res) => res.json('file_key') !== undefined,
    'presigned URL contains document ID': (res) => res.json('document_id') !== undefined,
  });

  return response;
}

/**
 * Confirms document upload completion.
 */
export function confirmUpload(token, documentId) {
  const params = {
    headers: {
      'Authorization': `Bearer ${token}`,
      'X-Request-ID': `k6-confirm-${__VU}-${__ITER}`,
    },
  };

  const response = http.post(`${BASE_URL}/documents/${documentId}/confirm`, null, params);

  check(response, {
    'confirm upload responded with 200': (res) => res.status === 200,
    'confirm upload sets completed status': (res) => res.json('status') === 'completed',
  });

  return response;
}
