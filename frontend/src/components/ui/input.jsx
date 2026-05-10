import { cn } from "../../lib/utils";

export function Input({ className, ...props }) {
  return (
    <input
      className={cn(
        "h-11 w-full rounded-md border border-[#7B1FFF]/38 bg-[#07030F]/72 px-3 text-sm text-[#F4EEFF] outline-none placeholder:text-[#B9A7D9]/65 focus:border-[#C063FF]/70",
        className
      )}
      {...props}
    />
  );
}
