import React, { useCallback, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  BackHandler,
  Platform,
  RefreshControl,
  SafeAreaView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from "react-native";
import { WebView } from "react-native-webview";
import { BASE_URL, FALLBACK_URLS } from "../config/appConfig";

export default function MainWebViewScreen() {
  const webViewRef = useRef(null);
  const [canGoBack, setCanGoBack] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [failedUrls, setFailedUrls] = useState([]);
  const [currentUrl, setCurrentUrl] = useState(BASE_URL);

  const availableFallback = useMemo(() => {
    return FALLBACK_URLS.find((url) => !failedUrls.includes(url));
  }, [failedUrls]);

  const handleBackPress = useCallback(() => {
    if (canGoBack && webViewRef.current) {
      webViewRef.current.goBack();
      return true;
    }

    Alert.alert("Exit App", "Do you want to close the app?", [
      { text: "Cancel", style: "cancel" },
      { text: "Exit", style: "destructive", onPress: () => BackHandler.exitApp() },
    ]);
    return true;
  }, [canGoBack]);

  React.useEffect(() => {
    if (Platform.OS !== "android") {
      return undefined;
    }
    const subscription = BackHandler.addEventListener("hardwareBackPress", handleBackPress);
    return () => subscription.remove();
  }, [handleBackPress]);

  const onRefresh = useCallback(() => {
    setIsRefreshing(true);
    webViewRef.current?.reload();
  }, []);

  const onNavigationStateChange = (state) => {
    setCanGoBack(state.canGoBack);
  };

  const onLoadStart = () => {
    setIsLoading(true);
  };

  const onLoadEnd = () => {
    setIsLoading(false);
    setIsRefreshing(false);
  };

  const onError = () => {
    setIsLoading(false);
    setIsRefreshing(false);

    if (availableFallback) {
      setFailedUrls((prev) => [...new Set([...prev, currentUrl])]);
      setCurrentUrl(availableFallback);
      return;
    }

    Alert.alert(
      "Connection Error",
      "Could not load the study platform. Check server URL and network."
    );
  };

  return (
    <SafeAreaView style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.title}>Study Platform</Text>
        <TouchableOpacity style={styles.button} onPress={onRefresh}>
          <Text style={styles.buttonText}>Refresh</Text>
        </TouchableOpacity>
      </View>

      <View style={styles.webviewContainer}>
        <WebView
          ref={webViewRef}
          source={{ uri: currentUrl }}
          onNavigationStateChange={onNavigationStateChange}
          onLoadStart={onLoadStart}
          onLoadEnd={onLoadEnd}
          onError={onError}
          startInLoadingState
          javaScriptEnabled
          domStorageEnabled
          sharedCookiesEnabled
          thirdPartyCookiesEnabled
          pullToRefreshEnabled
          cacheEnabled
          originWhitelist={["*"]}
          allowsInlineMediaPlayback
          mediaPlaybackRequiresUserAction={false}
          refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={onRefresh} />}
          renderLoading={() => (
            <View style={styles.loadingOverlay}>
              <ActivityIndicator size="large" color="#0f172a" />
              <Text style={styles.loadingText}>Loading platform...</Text>
            </View>
          )}
        />

        {isLoading ? (
          <View style={styles.progressBar} />
        ) : null}
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#ffffff",
  },
  header: {
    height: 56,
    paddingHorizontal: 16,
    borderBottomWidth: 1,
    borderBottomColor: "#e5e7eb",
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    backgroundColor: "#f8fafc",
  },
  title: {
    fontSize: 17,
    fontWeight: "700",
    color: "#0f172a",
  },
  button: {
    backgroundColor: "#0f172a",
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  buttonText: {
    color: "#ffffff",
    fontSize: 13,
    fontWeight: "600",
  },
  webviewContainer: {
    flex: 1,
  },
  loadingOverlay: {
    ...StyleSheet.absoluteFillObject,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "#ffffff",
  },
  loadingText: {
    marginTop: 12,
    color: "#334155",
  },
  progressBar: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    height: 2,
    backgroundColor: "#2563eb",
  },
});
