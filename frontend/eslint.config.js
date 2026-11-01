import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

// shortcut: remove the minimatch 11 override when its parent tools accept a patched version.
export default tseslint.config(
  { ignores: ["dist/**", "coverage/**", "node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    rules: { "no-console": "error" },
  },
  {
    files: ["**/*.test.{ts,tsx}", "src/test/**/*.{ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.vitest },
    },
  },
);
