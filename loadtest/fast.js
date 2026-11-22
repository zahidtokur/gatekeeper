// Scenario 1: fast, healthy upstream.
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export const options = {
  vus: 20,
  duration: '30s',
};

export default function () {
  const res = http.get(`${BASE_URL}/api/orders/1/`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
