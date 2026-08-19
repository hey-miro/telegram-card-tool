import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUTH_JS = ROOT / "static" / "auth.js"


class AuthActivationTests(unittest.TestCase):
    def test_successful_activation_enters_the_app(self):
        harness = r"""
const fs = require("fs");
const vm = require("vm");

const source = fs
  .readFileSync(process.argv[1], "utf8")
  .replace(/\ninit\(\);\s*$/, "\n");
const elements = {};
for (const id of [
  "toast",
  "statusText",
  "expireText",
  "modeText",
  "enterBtn",
  "licenseCode",
  "formError",
  "activateBtn",
  "activateForm",
  "showCode",
  "recheckBtn",
  "closeBtn",
]) {
  elements[id] = {
    textContent: "",
    className: "",
    hidden: true,
    disabled: true,
    value: id === "licenseCode" ? "TEST-CODE" : "",
    type: "",
    innerHTML: id === "activateBtn" ? "激活并进入软件" : "",
    dataset: {},
    addEventListener() {},
  };
}

const windowObject = { location: { href: "/auth-page" }, close() {} };
const responses = {
  "/api/license/activate": { ok: true },
  "/api/license/status": {
    status: "valid",
    expires_at: "2027-01-01T00:00:00",
  },
};
const context = {
  document: { getElementById: (id) => elements[id] },
  window: windowObject,
  fetch: async (path) => ({
    ok: true,
    json: async () => responses[path],
  }),
  AbortController,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
};

vm.createContext(context);
vm.runInContext(source, context);

(async () => {
  await context.activate();
  process.stdout.write(JSON.stringify({
    status: elements.statusText.textContent,
    enterEnabled: !elements.enterBtn.disabled,
    location: windowObject.location.href,
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(AUTH_JS)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual("授权有效", result["status"])
        self.assertTrue(result["enterEnabled"])
        self.assertEqual("/", result["location"])


if __name__ == "__main__":
    unittest.main()
