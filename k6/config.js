/**
 * Production Load Testing Configuration Profiles
 * Encapsulates target thresholds (SLAs), environments, and scaling profiles.
 */

// Target API Mount Point
export const BASE_URL = 'http://localhost:8000/api/v1';

// Strict Quality of Service (QoS) SLAs
export const PERFORMANCE_THRESHOLDS = {
  // Over 99% of requests must succeed
  'http_req_failed': ['rate<0.01'],
  // 95% of API requests must complete within 200ms
  'http_req_duration': ['p(95)<200'],
  // Connection setups must complete under 100ms
  'http_req_connecting': ['p(95)<100'],
};

// --- Execution Scenarios ---

// 1. Smoke Profile: Lightweight check to verify code paths are correct under minimal load
export const SMOKE_SCENARIO = {
  executor: 'constant-vus',
  vus: 3,
  duration: '30s',
};

// 2. Load Profile: Mimics standard concurrent users peak hours to ensure DB pool scales properly
export const LOAD_SCENARIO = {
  executor: 'ramping-vus',
  startVUs: 0,
  stages: [
    { duration: '30s', target: 20 },  // Ramp up from 0 to 20 VUs
    { duration: '1m', target: 20 },   // Sustain at 20 concurrent VUs
    { duration: '15s', target: 0 },   // Clean ramp down to 0
  ],
};

// 3. Stress Profile: Extreme scale to evaluate rate-limiter bounds and connection queuing limiters
export const STRESS_SCENARIO = {
  executor: 'ramping-vus',
  startVUs: 0,
  stages: [
    { duration: '30s', target: 120 }, // Spike directly to 120 VUs (beyond rate limit threshold)
    { duration: '1m', target: 120 },  // Maintain pressure
    { duration: '15s', target: 0 },   // Ramp down
  ],
};

// 4. Breakpoint Profile: Continuously increases load to locate the system saturation ceiling
export const BREAKPOINT_SCENARIO = {
  executor: 'ramping-vus',
  startVUs: 0,
  stages: [
    { duration: '3m', target: 600 },  // Continuously ramp up from 0 to 600 concurrent users
    { duration: '30s', target: 0 },   // Cool down ramp
  ],
};
