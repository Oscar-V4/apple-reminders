#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <ImageIO/ImageIO.h>
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

static NSString *normalize_uuid(NSString *value) {
    NSArray<NSString *> *prefixes = @[
        @"x-apple-reminder://",
        @"x-apple-reminderkit://"
    ];
    for (NSString *prefix in prefixes) {
        if ([value hasPrefix:prefix]) {
            return [value substringFromIndex:prefix.length];
        }
    }
    return value;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        BOOL removeMode = argc == 4 && [@"remove" isEqualToString:[NSString stringWithUTF8String:argv[1]]];
        if ((!removeMode && argc != 3) || (removeMode && argc != 4)) {
            write_json(@{
                @"ok": @NO,
                @"error": @"usage: remkit_attach_image REMINDER_UUID_OR_URL IMAGE_PATH | remove REMINDER_UUID ATTACHMENT_UUID"
            });
            return 2;
        }

        void *reminderKit = dlopen("/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit", RTLD_NOW);
        void *reminderKitInternal = dlopen("/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal", RTLD_NOW);
        if (!reminderKit && !reminderKitInternal) {
            const char *err = dlerror();
            write_json(@{@"ok": @NO, @"error": err ? [NSString stringWithUTF8String:err] : @"dlopen_failed"});
            return 2;
        }

        int reminderArgument = removeMode ? 2 : 1;
        NSString *uuidString = normalize_uuid([NSString stringWithUTF8String:argv[reminderArgument]]);
        NSString *attachmentUUIDString = removeMode
            ? normalize_uuid([NSString stringWithUTF8String:argv[3]])
            : nil;
        NSString *imagePath = removeMode ? nil : [NSString stringWithUTF8String:argv[2]];
        NSString *imageName = imagePath.lastPathComponent;
        NSData *imageData = nil;
        NSString *imageUTI = nil;
        NSUInteger width = 0;
        NSUInteger height = 0;
        NSError *error = nil;
        if (!removeMode) {
            NSURL *imageURL = [NSURL fileURLWithPath:imagePath];
            imageData = [NSData dataWithContentsOfURL:imageURL
                                              options:NSDataReadingMappedIfSafe
                                                error:&error];
            if (!imageData) {
                write_json(@{
                    @"ok": @NO,
                    @"error": @"image_data_load_failed",
                    @"detail": error.localizedDescription ?: @"unknown",
                    @"image": imageName
                });
                return 2;
            }
            CGImageSourceRef imageSource = CGImageSourceCreateWithData(
                (__bridge CFDataRef)imageData,
                NULL
            );
            CFStringRef sourceType = imageSource ? CGImageSourceGetType(imageSource) : NULL;
            imageUTI = sourceType ? [(__bridge NSString *)sourceType copy] : nil;
            if (imageSource) {
                CFRelease(imageSource);
            }
            NSSet<NSString *> *supportedImageUTIs = [NSSet setWithObjects:
                @"public.jpeg",
                @"public.png",
                nil
            ];
            if (![supportedImageUTIs containsObject:imageUTI]) {
                write_json(@{
                    @"ok": @NO,
                    @"error": @"unsupported_decoded_image_type",
                    @"detected_uti": imageUTI ?: [NSNull null],
                    @"image": imageName
                });
                return 2;
            }
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
            width = bitmap ? (NSUInteger)bitmap.pixelsWide : (NSUInteger)llround(image.size.width);
            height = bitmap ? (NSUInteger)bitmap.pixelsHigh : (NSUInteger)llround(image.size.height);
            if (width == 0 || height == 0) {
                write_json(@{@"ok": @NO, @"error": @"invalid_image_dimensions", @"image": imageName});
                return 2;
            }
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
        error = nil;
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
        id attachment = nil;
        NSUInteger attachmentsBefore = 0;
        if (removeMode) {
            NSUUID *attachmentUUID = [[NSUUID alloc] initWithUUIDString:attachmentUUIDString];
            if (!attachmentUUID) {
                write_json(@{@"ok": @NO, @"error": @"bad_attachment_uuid"});
                return 2;
            }
            if (![attachmentContext respondsToSelector:@selector(attachments)]
                || ![attachmentContext respondsToSelector:@selector(removeAttachment:)]) {
                write_json(@{@"ok": @NO, @"error": @"remove_attachment_selectors_missing"});
                return 2;
            }
            NSArray *attachments = send_id(attachmentContext, @selector(attachments));
            attachmentsBefore = attachments.count;
            for (id candidate in attachments) {
                if (![candidate respondsToSelector:@selector(objectID)]) continue;
                id candidateObjectID = send_id(candidate, @selector(objectID));
                if (![candidateObjectID respondsToSelector:@selector(uuid)]) continue;
                NSUUID *candidateUUID = send_id(candidateObjectID, @selector(uuid));
                if ([candidateUUID isEqual:attachmentUUID]) {
                    attachment = candidate;
                    break;
                }
            }
            if (!attachment) {
                write_json(@{
                    @"ok": @NO,
                    @"error": @"attachment_not_found",
                    @"attachment_count": @(attachmentsBefore)
                });
                return 1;
            }
            ((void (*)(id, SEL, id))objc_msgSend)(
                attachmentContext,
                @selector(removeAttachment:),
                attachment
            );
        } else {
            if (![attachmentContext respondsToSelector:@selector(addImageAttachmentWithData:uti:width:height:)]) {
                write_json(@{@"ok": @NO, @"error": @"attachment_selector_missing", @"reminder": uuidString});
                return 1;
            }
            attachment = ((id (*)(id, SEL, id, id, NSUInteger, NSUInteger))objc_msgSend)(
                attachmentContext,
                @selector(addImageAttachmentWithData:uti:width:height:),
                imageData,
                imageUTI,
                width,
                height
            );
            if (!attachment) {
                write_json(@{
                    @"ok": @NO,
                    @"error": @"add_image_attachment_failed",
                    @"reminder": uuidString,
                    @"image": imageName
                });
                return 1;
            }
        }

        error = nil;
        BOOL saved = ((BOOL (*)(id, SEL, NSError **))objc_msgSend)(saveRequest, @selector(saveSynchronouslyWithError:), &error);
        if (!saved) {
            if (removeMode) {
                write_json(@{
                    @"ok": @NO,
                    @"error": @"save_failed",
                    @"detail": error.localizedDescription ?: @"unknown",
                    @"operation": @"remove_attachment",
                    @"mutation_attempted": @YES
                });
            } else {
                write_json(@{
                    @"ok": @NO,
                    @"error": @"save_failed",
                    @"detail": error.localizedDescription ?: @"unknown",
                    @"reminder": uuidString,
                    @"image": imageName
                });
            }
            return 1;
        }

        if (removeMode) {
            write_json(@{
                @"ok": @YES,
                @"backend": @"reminderkit",
                @"operation": @"remove_attachment",
                @"reminder_id": uuidString,
                @"attachment_id": attachmentUUIDString,
                @"attachments_before": @(attachmentsBefore),
                @"mutation_attempted": @YES
            });
            return 0;
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
            @"attachment_transport": @"data",
            @"image_uti": imageUTI,
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
