import { useQuery } from "@tanstack/react-query";
import { Text, View } from "react-native";

import { getHistory } from "@/lib/api/history";
import { ApiError } from "@/lib/api/client";

export default function HistoryScreen() {
  const historyQuery = useQuery({ queryKey: ["history"], queryFn: getHistory, retry: false });

  // GET /history is a real 501 on the backend today — no settled outcomes exist yet to
  // aggregate accuracy stats from (see backend/app/history/router.py). Showing that
  // honestly rather than faking a populated screen.
  const isNotImplemented =
    historyQuery.error instanceof ApiError && historyQuery.error.status === 501;

  return (
    <View className="flex-1 items-center justify-center bg-white px-8 dark:bg-black">
      {historyQuery.isLoading && <Text className="text-gray-400">Loading…</Text>}
      {isNotImplemented && (
        <Text className="text-center text-gray-500 dark:text-gray-400">
          Prediction history isn&apos;t available yet — the backend hasn&apos;t settled any
          outcomes yet.
        </Text>
      )}
      {historyQuery.isError && !isNotImplemented && (
        <Text className="text-center text-red-500">Couldn&apos;t reach the SportPIQ API.</Text>
      )}
    </View>
  );
}
