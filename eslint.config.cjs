// This file is part of adversarial-review-loop.
//
// Copyright (c) 2026 Roberto Leinardi
//
// adversarial-review-loop is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// adversarial-review-loop is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.

// ESLint for the display module and its tests -- the only JavaScript in the repository.
//
// Run by the `eslint` hook in .pre-commit-config.yaml, from the node environment pre-commit
// builds out of that hook's pinned `additional_dependencies`. There is no package.json: the
// plugin works straight from a checkout, and its one development toolchain is pre-commit.
// CommonJS, because `require` honours the NODE_PATH that environment sets and an ES module
// `import` does not.

const js = require('@eslint/js')
const tseslint = require('typescript-eslint')

// The rules this author's other JavaScript repositories enforce beyond `recommended`.
const STYLE = {
  'consistent-return': 'error',
  eqeqeq: ['error', 'smart'],
  'no-var': 'error',
  'prefer-arrow-callback': 'error',
  'prefer-const': ['error', { destructuring: 'all' }],
}

// A hooks module runs with no DOM and no Node; `h` is the one global the engine gives it.
const MODULE_GLOBALS = { h: 'readonly' }

module.exports = tseslint.config(
  {
    ignores: ['.claude-plugin/types/', 'scripts/', '.mk/'],
  },
  {
    files: ['hooks/**/*.js'],
    extends: [js.configs.recommended],
    languageOptions: { ecmaVersion: 'latest', sourceType: 'module', globals: MODULE_GLOBALS },
    rules: STYLE,
  },
  {
    files: ['tests/mod/**/*.ts'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { globals: MODULE_GLOBALS },
    rules: STYLE,
  },
)
