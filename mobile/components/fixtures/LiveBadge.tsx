import { Text, View } from "react-native";

export function LiveBadge() {
  return (
    <View className="flex-row items-center rounded-full bg-red-600 px-2 py-0.5">
      <View className="mr-1 h-1.5 w-1.5 rounded-full bg-white" />
      <Text className="text-xs font-bold text-white">LIVE</Text>
    </View>
  );
}
