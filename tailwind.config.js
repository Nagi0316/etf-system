/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './templates/**/*.html',
    './static/js/**/*.js',
  ],
  safelist: [
    // 動態顏色類（JS 依據漲跌方向動態賦予）
    'text-red-500', 'text-green-500', 'text-red-400', 'text-green-400',
    'text-blue-500', 'text-amber-500', 'text-purple-500', 'text-indigo-500',
    'text-emerald-600', 'text-slate-400', 'text-slate-500',
    'bg-red-50', 'bg-green-50', 'bg-red-900/20', 'bg-green-900/20',
    'bg-indigo-50', 'bg-amber-50', 'bg-purple-50', 'bg-sky-50', 'bg-rose-50',
    'dark:bg-red-900/20', 'dark:bg-green-900/20', 'dark:bg-indigo-900/40',
    // 動態寬度（評分條）
    { pattern: /^w-\[?[0-9]/ },
    // 動態高亮 nav
    'bg-indigo-50', 'text-indigo-600', 'font-medium',
    'dark:bg-indigo-900/40', 'dark:text-indigo-400',
    // pulse animation
    'pulse-dot', 'open', 'close',
    // grade pill colors (JS style= 使用，無需 safelist)
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
