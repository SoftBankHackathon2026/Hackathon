import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PIN_RETRY_MESSAGE,
  pinDigits,
  submitPin,
} from "../src/features/access-pin/pin.ts";

test("PIN 입력은 숫자 4자리까지만 남긴다", () => {
  assert.equal(pinDigits("12"), "12");
  assert.equal(pinDigits("1a2-3 4"), "1234");
  assert.equal(pinDigits("123456"), "1234");
  assert.equal(pinDigits("abcd"), "");
});

test("PIN 제출이 네트워크 오류로 실패해도 다시 시도할 수 있는 메시지를 돌려준다", async () => {
  const saved = globalThis.fetch;
  try {
    globalThis.fetch = async () => {
      throw new TypeError("Failed to fetch");
    };
    assert.equal(await submitPin("1234"), PIN_RETRY_MESSAGE);
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: "틀렸어요" }), { status: 401 });
    assert.equal(await submitPin("1234"), "틀렸어요");
    globalThis.fetch = async () => new Response("{}", { status: 200 });
    assert.equal(await submitPin("1234"), null);
  } finally {
    globalThis.fetch = saved;
  }
});
