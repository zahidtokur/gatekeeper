// Scenario 2: intentionally slow upstream (1 s delay, below the 5 s read timeout).
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const DELAY = __ENV.DELAY || '1';

export const options = {
  vus: 20,
  duration: '30s',
};

export default function () {
  const res = http.get(`${BASE_URL}/api/orders/slow/?seconds=${DELAY}`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
