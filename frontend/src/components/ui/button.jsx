import { cva } from "class-variance-authority";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#C063FF]/50 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "border border-[#7B1FFF]/35 bg-[#9B35FF]/18 text-[#F4EEFF] hover:bg-[#9B35FF]/28",
        primary: "border border-[#7B1FFF]/45 bg-[#9B35FF] text-[#F4EEFF] hover:bg-[#C063FF]",
        ghost: "text-[#B9A7D9] hover:bg-[#7B1FFF]/18 hover:text-[#F4EEFF]",
        outline: "border border-[#7B1FFF]/45 bg-transparent text-[#F4EEFF] hover:bg-[#7B1FFF]/15",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-10 px-4 py-2",
        lg: "h-12 px-5 text-base",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  }
);

export function Button({ className, variant, size, ...props }) {
  return <button className={cn(buttonVariants({ variant, size, className }))} {...props} />;
}
