#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <objc/runtime.h>
#import <dlfcn.h>

static id send_id(id target, SEL sel) {
    return ((id (*)(id, SEL))objc_msgSend)(target, sel);
}

static void send_void_bool(id target, SEL sel, BOOL value) {
    ((void (*)(id, SEL, BOOL))objc_msgSend)(target, sel, value);
}

static void write_json(NSDictionary *payload) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    if (!data) {
        printf("{\"ok\":false,\"error\":\"json_serialization_failed\"}\n");
        return;
    }
    fwrite(data.bytes, 1, data.length, stdout);
    printf("\n");
}

static NSString *normalize_reminder_uuid(NSString *value) {
    NSString *prefix = @"x-apple-reminder://";
    if ([value hasPrefix:prefix]) {
        return [value substringFromIndex:prefix.length];
    }
    return value;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc < 3) {
            write_json(@{@"ok": @NO, @"error": @"usage: remkit_attach_image REMINDER_UUID_OR_URL IMAGE_PATH"});
            return 2;
        }

        void *reminderKit = dlopen("/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit", RTLD_NOW);
        void *reminderKitInternal = dlopen("/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal", RTLD_NOW);
        if (!reminderKit && !reminderKitInternal) {
            const char *err = dlerror();
            write_json(@{@"ok": @NO, @"error": err ? [NSString stringWithUTF8String:err] : @"dlopen_failed"});
            return 2;
        }

        NSString *uuidString = normalize_reminder_uuid([NSString stringWithUTF8String:argv[1]]);
        NSString *imagePath = [NSString stringWithUTF8String:argv[2]];
        NSString *imageName = imagePath.lastPathComponent;
        NSURL *imageURL = [NSURL fileURLWithPath:imagePath];
        NSImage *image = [[NSImage alloc] initWithContentsOfURL:imageURL];
        if (!image) {
            write_json(@{@"ok": @NO, @"error": @"image_load_failed", @"image": imageName});
            return 2;
        }

        NSBitmapImageRep *bitmap = nil;
        for (NSImageRep *candidate in image.representations) {
            if ([candidate isKindOfClass:NSBitmapImageRep.class]) {
                bitmap = (NSBitmapImageRep *)candidate;
                break;
            }
        }
        NSUInteger width = bitmap ? (NSUInteger)bitmap.pixelsWide : (NSUInteger)llround(image.size.width);
        NSUInteger height = bitmap ? (NSUInteger)bitmap.pixelsHigh : (NSUInteger)llround(image.size.height);
        if (width == 0 || height == 0) {
            write_json(@{@"ok": @NO, @"error": @"invalid_image_dimensions", @"image": imageName});
            return 2;
        }

        Class REMStore = NSClassFromString(@"REMStore");
        Class REMReminder = NSClassFromString(@"REMReminder");
        Class REMSaveRequest = NSClassFromString(@"REMSaveRequest");
        if (!REMStore || !REMReminder || !REMSaveRequest) {
            write_json(@{@"ok": @NO, @"error": @"required_reminderkit_classes_missing"});
            return 2;
        }
        if (![REMReminder respondsToSelector:@selector(objectIDWithUUID:)]
            || ![REMStore instancesRespondToSelector:@selector(init)]
            || ![REMStore instancesRespondToSelector:@selector(fetchReminderWithObjectID:error:)]
            || ![REMSaveRequest instancesRespondToSelector:@selector(initWithStore:)]
            || ![REMSaveRequest instancesRespondToSelector:@selector(setSyncToCloudKit:)]
            || ![REMSaveRequest instancesRespondToSelector:@selector(updateReminder:)]
            || ![REMSaveRequest instancesRespondToSelector:@selector(saveSynchronouslyWithError:)]) {
            write_json(@{@"ok": @NO, @"error": @"required_reminderkit_selectors_missing"});
            return 2;
        }

        id store = send_id(send_id((id)REMStore, @selector(alloc)), @selector(init));
        NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:uuidString];
        if (!uuid) {
            write_json(@{@"ok": @NO, @"error": @"bad_reminder_uuid", @"reminder": uuidString});
            return 2;
        }

        id objectID = ((id (*)(id, SEL, id))objc_msgSend)((id)REMReminder, @selector(objectIDWithUUID:), uuid);
        NSError *error = nil;
        id reminder = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(store, @selector(fetchReminderWithObjectID:error:), objectID, &error);
        if (!reminder) {
            write_json(@{
                @"ok": @NO,
                @"error": @"fetch_reminder_failed",
                @"detail": error.localizedDescription ?: @"unknown",
                @"reminder": uuidString
            });
            return 1;
        }

        id saveRequest = ((id (*)(id, SEL, id))objc_msgSend)(send_id((id)REMSaveRequest, @selector(alloc)), @selector(initWithStore:), store);
        send_void_bool(saveRequest, @selector(setSyncToCloudKit:), YES);
        id changeItem = ((id (*)(id, SEL, id))objc_msgSend)(saveRequest, @selector(updateReminder:), reminder);
        if (!changeItem) {
            write_json(@{@"ok": @NO, @"error": @"update_reminder_returned_nil", @"reminder": uuidString});
            return 1;
        }

        if (![changeItem respondsToSelector:@selector(attachmentContext)]) {
            write_json(@{@"ok": @NO, @"error": @"attachment_context_selector_missing", @"reminder": uuidString});
            return 1;
        }
        id attachmentContext = send_id(changeItem, @selector(attachmentContext));
        if (!attachmentContext) {
            write_json(@{@"ok": @NO, @"error": @"attachment_context_nil", @"reminder": uuidString});
            return 1;
        }
        if (![attachmentContext respondsToSelector:@selector(addImageAttachmentWithURL:width:height:error:)]) {
            write_json(@{@"ok": @NO, @"error": @"attachment_selector_missing", @"reminder": uuidString});
            return 1;
        }

        error = nil;
        id attachment = ((id (*)(id, SEL, id, NSUInteger, NSUInteger, NSError **))objc_msgSend)(
            attachmentContext,
            @selector(addImageAttachmentWithURL:width:height:error:),
            imageURL,
            width,
            height,
            &error
        );
        if (!attachment) {
            write_json(@{
                @"ok": @NO,
                @"error": @"add_image_attachment_failed",
                @"detail": error.localizedDescription ?: @"unknown",
                @"reminder": uuidString,
                @"image": imageName
            });
            return 1;
        }

        error = nil;
        BOOL saved = ((BOOL (*)(id, SEL, NSError **))objc_msgSend)(saveRequest, @selector(saveSynchronouslyWithError:), &error);
        if (!saved) {
            write_json(@{
                @"ok": @NO,
                @"error": @"save_failed",
                @"detail": error.localizedDescription ?: @"unknown",
                @"reminder": uuidString,
                @"image": imageName
            });
            return 1;
        }

        if ([store respondsToSelector:@selector(triggerCloudKitOnlySyncWithReason:discretionary:completion:)]) {
            ((void (*)(id, SEL, id, BOOL, id))objc_msgSend)(
                store,
                @selector(triggerCloudKitOnlySyncWithReason:discretionary:completion:),
                @"codex-reminderkit-image-attachment",
                NO,
                nil
            );
        }

        NSString *attachmentID = nil;
        if ([attachment respondsToSelector:@selector(objectID)]) {
            id attachmentObjectID = send_id(attachment, @selector(objectID));
            if ([attachmentObjectID respondsToSelector:@selector(uuid)]) {
                NSUUID *attachmentUUID = send_id(attachmentObjectID, @selector(uuid));
                attachmentID = attachmentUUID.UUIDString;
            }
        }

        NSMutableDictionary *payload = [@{
            @"ok": @YES,
            @"backend": @"reminderkit",
            @"reminder_id": uuidString,
            @"image": imageName,
            @"width": @(width),
            @"height": @(height)
        } mutableCopy];
        if (attachmentID) {
            payload[@"attachment_id"] = attachmentID;
        }
        write_json(payload);
        return 0;
    }
}
