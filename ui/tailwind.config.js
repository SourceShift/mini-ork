/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        ink: {
          50: "#f8f9fa",
          100: "#e9ecef",
          200: "#dee2e6",
          300: "#adb5bd",
          400: "#6c757d",
          500: "#495057",
          600: "#343a40",
          700: "#212529",
          800: "#16191c",
          900: "#0d0f10",
          950: "#08090a",
        },
        ork: {
          // Brand-ish red+green palette pulled from the mini-ork hero image:
          // red master orc & green mini-orcs. Subtle, not flashy.
          red: "#8b1e1e",
          green: "#1e6b3a",
          amber: "#b07a2c",
          slate: "#3a4250",
        },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
        sans: ["Inter", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
