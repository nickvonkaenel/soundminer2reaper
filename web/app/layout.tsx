import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "soundminer2reaper — Browser preset converter",
  description:
    "Convert Soundminer VST2 and VST3 presets into REAPER FX chains without uploading your files.",
  openGraph: {
    title: "Soundminer → REAPER, right in your browser",
    description:
      "Drop in your preset database and download organized REAPER FX chains. Files never leave your device.",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "Soundminer → REAPER, right in your browser",
    description:
      "Convert VST2 and VST3 presets locally and download the chains as a ZIP.",
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
