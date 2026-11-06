import assert from "node:assert/strict";
import { test } from "node:test";
import { pinDigits } from "../src/features/access-pin/pin.ts";

test("PIN 입력은 숫자 4자리까지만 남긴다", () => {
  assert.equal(pinDigits("12"), "12");
  assert.equal(pinDigits("1a2-3 4"), "1234");
  assert.equal(pinDigits("123456"), "1234");
  assert.equal(pinDigits("abcd"), "");
});
