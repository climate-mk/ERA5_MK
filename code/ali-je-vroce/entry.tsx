import { render } from "solid-js/web";
import { QueryClientProvider, QueryClient } from "@tanstack/solid-query";
import { AliJeVroce } from "./AliJeVroce.tsx";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 1000 * 60 * 5, gcTime: 1000 * 60 * 60 },
  },
});

const root = document.getElementById("ali-je-vroce");
if (root) {
  render(
    () => (
      <QueryClientProvider client={queryClient}>
        <AliJeVroce />
      </QueryClientProvider>
    ),
    root
  );
}
