import React from "react";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import MainWebViewScreen from "./src/screens/MainWebViewScreen";

export default function App() {
  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
      <MainWebViewScreen />
    </SafeAreaProvider>
  );
}
