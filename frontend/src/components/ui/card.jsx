import { cn } from "../../lib/utils";

export function Card({ className, ...props }) {
  return (
    <div
      className={cn(
        "rounded-xl border border-[#7B1FFF]/28 bg-[#120522]/72 shadow-[0_10px_30px_rgba(155,53,255,0.08)]",
        className
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }) {
  return <div className={cn("border-b border-[#7B1FFF]/18 px-4 py-3", className)} {...props} />;
}

export function CardContent({ className, ...props }) {
  return <div className={cn("p-4", className)} {...props} />;
}
