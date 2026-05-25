/** @type {import('tailwindcss').Config} */
module.exports = {
  // 掃描所有 Jinja2 模板與 JS 檔案，確保 purge 不漏掉任何 class
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
  ],
  // 與 base.html 原本的 tailwind.config = { darkMode:'class' } 保持一致
  darkMode: "class",
  theme: { extend: {} },
  plugins: [],
};
