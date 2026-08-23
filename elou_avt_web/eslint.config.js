// eslint.config.js
//
// Added for the GitLab CI pipeline's mandatory ESLint check -- this project
// had NO ESLint configuration at all before (confirmed: no .eslintrc*, no
// eslint.config.*, no `eslint` devDependency, no `lint` script). This is
// the minimal, standard flat-config setup for this exact stack (React 18 +
// TypeScript + Vite), matching what Vite's own official react-ts template
// ships by default -- not a custom/opinionated rule set invented for CI.
//
// Run locally: npm run lint

import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
      // Matches tsconfig.json's own noUnusedLocals/noUnusedParameters: false
      // -- this codebase deliberately doesn't enforce that today; keeping
      // ESLint consistent with the existing TypeScript config rather than
      // introducing a stricter rule the project hasn't opted into.
      '@typescript-eslint/no-unused-vars': 'off',
    },
  },
);
