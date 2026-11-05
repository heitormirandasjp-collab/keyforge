const PATHS = {
  key: [
    "M14.5 9.5a3.5 3.5 0 1 1-2.6 5.9L9 18.3H6v-3l2.9-2.9a3.5 3.5 0 0 1 5.6-2.9Z",
    "M15 9h.01",
  ],
  menu: ["M4 7h16", "M4 12h16", "M4 17h16"],
  history: ["M4 12a8 8 0 1 0 2.3-5.6", "M4 4v4h4", "M12 8v4l3 2"],
  sliders: ["M4 7h9", "M17 7h3", "M4 17h3", "M11 17h9", "M15 5v4", "M9 15v4"],
  refresh: [
    "M20 12a8 8 0 0 1-13.6 5.7",
    "M4 12a8 8 0 0 1 13.6-5.7",
    "M20 4v4h-4",
    "M4 20v-4h4",
  ],
  swap: ["M7 4v12", "M7 16l-3-3", "M7 16l3-3", "M17 20V8", "M17 8l-3 3", "M17 8l3 3"],
  folder: [
    "M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5Z",
  ],
  trash: ["M4 7h16", "M9 7V5h6v2", "M6 7l1 13h10l1-13", "M10 11v6", "M14 11v6"],
  close: ["M6 6l12 12", "M18 6L6 18"],
  chevron: ["M6 9l6 6 6-6"],
  plus: ["M12 5v14", "M5 12h14"],
  compass: [
    "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z",
    "M14.6 9.4l-1.9 5.3-5.3 1.9 1.9-5.3Z",
  ],
  bolt: ["M13 2 4 14h7l-1 8 9-12h-7Z"],
  check: ["M4 12.5l5 5L20 6.5"],
  x: ["M6 6l12 12", "M18 6L6 18"],
  sparkle: ["M12 3l1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7Z"],
  sun: [
    "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z",
    "M12 2v2",
    "M12 20v2",
    "M4.9 4.9l1.4 1.4",
    "M17.7 17.7l1.4 1.4",
    "M2 12h2",
    "M20 12h2",
    "M4.9 19.1l1.4-1.4",
    "M17.7 6.3l1.4-1.4",
  ],
  moon: ["M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z"],
};

export default function Icon({ name, size = 16, className = "" }) {
  const paths = PATHS[name];
  if (!paths) return null;
  return (
    <svg
      className={`icon ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
