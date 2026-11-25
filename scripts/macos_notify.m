/* Native local notifications. All text arrives as JSON data, never as code. */
#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <UserNotifications/UserNotifications.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        @try {
            NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
            if (input.length == 0) {
                // LaunchServices opens this app when a delivered notification is clicked.
                // Bring the main application back instead of silently dropping that click.
                NSURL *mainApp = NSBundle.mainBundle.bundleURL;
                for (int i = 0; i < 3; i++) mainApp = mainApp.URLByDeletingLastPathComponent;
                return [NSWorkspace.sharedWorkspace openURL:mainApp] ? 0 : 2;
            }
            NSDictionary *request = [NSJSONSerialization JSONObjectWithData:input options:0 error:nil];
            if (![request isKindOfClass:[NSDictionary class]]) return 2;
            NSApplication *application = NSApplication.sharedApplication;
            [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
            [application finishLaunching];
            NSString *operation = request[@"operation"];
            if ([operation isEqualToString:@"authorize"]) [application activateIgnoringOtherApps:YES];
            UNUserNotificationCenter *center = UNUserNotificationCenter.currentNotificationCenter;
            __block BOOL finished = NO;
            __block NSMutableDictionary *result = [NSMutableDictionary dictionary];
            void (^settings)(void) = ^{
                [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *value) {
                    @synchronized(result) { result[@"authorization"] = @(value.authorizationStatus); }
                    if (![operation isEqualToString:@"send"]) { finished = YES; return; }
                    if (value.authorizationStatus != UNAuthorizationStatusAuthorized &&
                        value.authorizationStatus != UNAuthorizationStatusProvisional) {
                        result[@"delivered"] = @NO; finished = YES; return;
                    }
                    NSString *title = request[@"title"], *body = request[@"body"];
                    if (![title isKindOfClass:[NSString class]] || ![body isKindOfClass:[NSString class]]) {
                        result[@"delivered"] = @NO; finished = YES; return;
                    }
                    UNMutableNotificationContent *content = [UNMutableNotificationContent new];
                    content.title = title;
                    content.body = body;
                    content.sound = UNNotificationSound.defaultSound;
                    UNNotificationRequest *notification = [UNNotificationRequest
                        requestWithIdentifier:NSUUID.UUID.UUIDString content:content trigger:nil];
                    [center addNotificationRequest:notification withCompletionHandler:^(NSError *error) {
                        result[@"delivered"] = error ? @NO : @YES;
                        if (error) result[@"error"] = error.localizedDescription;
                        finished = YES;
                    }];
                }];
            };
            if ([operation isEqualToString:@"authorize"]) {
                [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert | UNAuthorizationOptionSound)
                    completionHandler:^(BOOL granted, NSError *error) {
                        if (error) result[@"error"] = error.localizedDescription;
                        settings();
                    }];
            } else if ([operation isEqualToString:@"status"] || [operation isEqualToString:@"send"]) {
                settings();
            } else {
                return 2;
            }
            NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:
                [operation isEqualToString:@"authorize"] ? 120 : 30];
            while (!finished && deadline.timeIntervalSinceNow > 0) {
                [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode
                    beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
            }
            if (!finished) return 3;
            NSData *output = [NSJSONSerialization dataWithJSONObject:result options:0 error:nil];
            [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
            return 0;
        } @catch (NSException *error) {
            return 4;
        }
    }
}
