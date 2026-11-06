import assert from "node:assert/strict";
import test from "node:test";
import type { ComponentEndpoint, EnvComponent } from "../src/api/migrations.ts";
import {
  canOpen,
  displayUrl,
  endpointWarning,
  primaryEndpoint,
  sortedEndpoints,
} from "../src/features/environments/endpointModel.ts";

function c(endpoints: ComponentEndpoint[]): EnvComponent {
  return {
    id: "x",
    name: "x",
    layer: "platform",
    kind: "registry",
    managed: false,
    status: "healthy",
    detail: "",
    depends_on: [],
    endpoints,
  };
}

const nodePort: ComponentEndpoint = {
  url: "http://192.0.2.10:30002/",
  source: "node_port",
  exposure: "private",
};
const ingress: ComponentEndpoint = {
  url: "https://harbor.example.com",
  source: "ingress",
  exposure: "public",
  label: "웹 UI",
};
const internal: ComponentEndpoint = {
  url: "http://harbor.harbor.svc:80",
  source: "cluster_ip",
  exposure: "private",
};

test("sortedEndpoints puts public https first and cluster addresses last", () => {
  assert.deepEqual(
    sortedEndpoints(c([internal, nodePort, ingress])).map((e) => e.source),
    ["ingress", "node_port", "cluster_ip"],
  );
});

test("only http(s) addresses outside the cluster can be opened", () => {
  assert.equal(canOpen(nodePort), true);
  assert.equal(canOpen(internal), false);
  assert.equal(
    canOpen({
      url: "javascript:alert(1)",
      source: "ingress",
      exposure: "public",
    }),
    false,
  );
  assert.equal(primaryEndpoint(c([internal, nodePort]))?.url, nodePort.url);
  assert.equal(primaryEndpoint(c([internal])), null);
  assert.equal(primaryEndpoint(c([])), null);
});

test("public plain http gets a warning and urls lose the trailing slash", () => {
  assert.equal(
    endpointWarning({ ...nodePort, exposure: "public" }),
    "암호화 안 됨(HTTP)",
  );
  assert.equal(endpointWarning(nodePort), null);
  assert.equal(displayUrl(nodePort.url), "http://192.0.2.10:30002");
});
