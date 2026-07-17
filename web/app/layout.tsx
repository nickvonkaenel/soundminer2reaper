import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "soundminer2reaper",
  description:
    "Convert Soundminer VST2 and VST3 presets into REAPER FX chains in your browser.",
  openGraph: {
    title: "soundminer2reaper",
    description:
      "Convert Soundminer preset databases and exports into REAPER FX chains.",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "soundminer2reaper",
    description:
      "Convert Soundminer presets into REAPER FX chains in your browser.",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
