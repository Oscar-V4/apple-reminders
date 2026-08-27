#import <Foundation/Foundation.h>
#import <CommonCrypto/CommonDigest.h>
#import <objc/message.h>
#import <dlfcn.h>

static id Send0(id target, SEL selector) {
    return ((id (*)(id, SEL))objc_msgSend)(target, selector);
}

static id Send1(id target, SEL selector, id value) {
    return ((id (*)(id, SEL, id))objc_msgSend)(target, selector, value);
}

static NSString *ObjectUUID(id object) {
    if (![object respondsToSelector:@selector(objectID)]) return nil;
    id objectID = Send0(object, @selector(objectID));
    if (![objectID respondsToSelector:@selector(uuid)]) return nil;
    NSUUID *uuid = Send0(objectID, @selector(uuid));
    return uuid.UUIDString;
}

static NSString *SHA256Hex(NSData *data) {
    if (!data) return nil;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, digest);
    NSMutableString *hex = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (NSUInteger index = 0; index < CC_SHA256_DIGEST_LENGTH; index++) {
        [hex appendFormat:@"%02x", digest[index]];
    }
    return hex;
}

static NSString *NativeGuardDigest(id reminder, NSError **error) {
    if (![reminder respondsToSelector:@selector(storage)]) return nil;
    id storage = Send0(reminder, @selector(storage));
    if (!storage) return nil;
    NSData *archive = [NSKeyedArchiver archivedDataWithRootObject:storage
                                           requiringSecureCoding:NO
                                                           error:error];
    return SHA256Hex(archive);
}

static void WriteJSON(NSDictionary *payload) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload
                                                   options:NSJSONWritingSortedKeys
                                                     error:&error];
    if (!data) {
        printf("{\"ok\":false,\"error\":\"json_serialization_failed\"}\n");
        return;
    }
    fwrite(data.bytes, 1, data.length, stdout);
    printf("\n");
}

static int Fail(NSString *code, NSError *error, BOOL mutationAttempted) {
    NSMutableDictionary *payload = [@{
        @"ok": @NO,
        @"error": code ?: @"unknown_error",
        @"mutation_attempted": @(mutationAttempted)
    } mutableCopy];
    if (error.localizedDescription) payload[@"detail"] = error.localizedDescription;
    WriteJSON(payload);
    return mutationAttempted ? 1 : 2;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        BOOL guardMode = argc == 3 &&
            [@"guard" isEqualToString:[NSString stringWithUTF8String:argv[1]]];
        BOOL recoverMode = argc == 5 &&
            [@"recover" isEqualToString:[NSString stringWithUTF8String:argv[1]]];
        if (!guardMode && !recoverMode) return Fail(@"usage", nil, NO);

        NSString *reminderUUIDString = [NSString stringWithUTF8String:argv[2]];
        NSString *listUUIDString = recoverMode
            ? [NSString stringWithUTF8String:argv[3]]
            : nil;
        NSString *expectedGuardDigest = recoverMode
            ? [NSString stringWithUTF8String:argv[4]]
            : nil;
        NSUUID *reminderUUID = [[NSUUID alloc] initWithUUIDString:reminderUUIDString];
        NSUUID *listUUID = recoverMode
            ? [[NSUUID alloc] initWithUUIDString:listUUIDString]
            : nil;
        if (!reminderUUID || (recoverMode && (!listUUID || expectedGuardDigest.length != 64))) {
            return Fail(@"invalid_arguments", nil, NO);
        }

        void *reminderKit = dlopen(
            "/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit",
            RTLD_NOW
        );
        void *reminderKitInternal = dlopen(
            "/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal",
            RTLD_NOW
        );
        if (!reminderKit && !reminderKitInternal) return Fail(@"dlopen_failed", nil, NO);

        Class REMStore = NSClassFromString(@"REMStore");
        Class REMReminder = NSClassFromString(@"REMReminder");
        Class REMList = NSClassFromString(@"REMList");
        Class REMSaveRequest = NSClassFromString(@"REMSaveRequest");
        if (!REMStore || !REMReminder || !REMList || !REMSaveRequest) {
            return Fail(@"required_reminderkit_classes_missing", nil, NO);
        }
        if (![REMReminder respondsToSelector:@selector(objectIDWithUUID:)] ||
            ![REMReminder instancesRespondToSelector:@selector(storage)] ||
            ![REMList respondsToSelector:@selector(objectIDWithUUID:)] ||
            ![REMStore instancesRespondToSelector:@selector(fetchReminderIncludingMarkedForDeleteWithObjectID:error:)] ||
            ![REMStore instancesRespondToSelector:@selector(fetchReminderWithObjectID:error:)] ||
            ![REMStore instancesRespondToSelector:@selector(fetchListWithObjectID:error:)] ||
            ![REMSaveRequest instancesRespondToSelector:@selector(initWithStore:)] ||
            ![REMSaveRequest instancesRespondToSelector:@selector(updateList:)] ||
            ![REMSaveRequest instancesRespondToSelector:@selector(setSyncToCloudKit:)] ||
            ![REMSaveRequest instancesRespondToSelector:@selector(saveSynchronouslyWithError:)]) {
            return Fail(@"required_reminderkit_selectors_missing", nil, NO);
        }

        id store = [[REMStore alloc] init];
        if (!store) return Fail(@"store_initialization_failed", nil, NO);
        id reminderObjectID = Send1(REMReminder, @selector(objectIDWithUUID:), reminderUUID);
        id listObjectID = recoverMode
            ? Send1(REMList, @selector(objectIDWithUUID:), listUUID)
            : nil;
        NSError *error = nil;
        id deletedReminder = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(
            store,
            @selector(fetchReminderIncludingMarkedForDeleteWithObjectID:error:),
            reminderObjectID,
            &error
        );
        if (!deletedReminder) return Fail(@"deleted_reminder_not_found", error, NO);

        NSError *guardError = nil;
        NSString *nativeGuardDigest = NativeGuardDigest(deletedReminder, &guardError);
        if (!nativeGuardDigest) return Fail(@"native_guard_unavailable", guardError, NO);
        if (guardMode) {
            WriteJSON(@{
                @"ok": @YES,
                @"operation": @"read_recovery_guard",
                @"mutation_attempted": @NO,
                @"reminder_id": reminderUUIDString,
                @"native_guard_digest": nativeGuardDigest
            });
            return 0;
        }
        if (![nativeGuardDigest isEqualToString:expectedGuardDigest]) {
            return Fail(@"concurrent_modification", nil, NO);
        }

        error = nil;
        id destinationList = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(
            store,
            @selector(fetchListWithObjectID:error:),
            listObjectID,
            &error
        );
        if (!destinationList) return Fail(@"destination_list_not_found", error, NO);

        id reminderAccount = [deletedReminder respondsToSelector:@selector(account)]
            ? Send0(deletedReminder, @selector(account))
            : nil;
        id listAccount = [destinationList respondsToSelector:@selector(account)]
            ? Send0(destinationList, @selector(account))
            : nil;
        NSString *reminderAccountID = ObjectUUID(reminderAccount);
        NSString *listAccountID = ObjectUUID(listAccount);
        if (!reminderAccountID || ![reminderAccountID isEqualToString:listAccountID]) {
            return Fail(@"cross_account_restore_not_supported", nil, NO);
        }
        id capabilities = [reminderAccount respondsToSelector:@selector(capabilities)]
            ? Send0(reminderAccount, @selector(capabilities))
            : nil;
        if (![capabilities respondsToSelector:@selector(supportsRecentlyDeletedList)] ||
            !((BOOL (*)(id, SEL))objc_msgSend)(capabilities, @selector(supportsRecentlyDeletedList))) {
            return Fail(@"recently_deleted_not_supported", nil, NO);
        }

        id saveRequest = ((id (*)(id, SEL, id))objc_msgSend)(
            [REMSaveRequest alloc], @selector(initWithStore:), store
        );
        if (!saveRequest) return Fail(@"save_request_initialization_failed", nil, NO);
        ((void (*)(id, SEL, BOOL))objc_msgSend)(
            saveRequest, @selector(setSyncToCloudKit:), YES
        );
        id listChange = Send1(saveRequest, @selector(updateList:), destinationList);
        if (![listChange respondsToSelector:@selector(undeleteReminderWithID:usingUndo:)]) {
            return Fail(@"undelete_selector_missing", nil, NO);
        }
        ((void (*)(id, SEL, id, id))objc_msgSend)(
            listChange,
            @selector(undeleteReminderWithID:usingUndo:),
            reminderObjectID,
            nil
        );

        error = nil;
        BOOL saved = ((BOOL (*)(id, SEL, NSError **))objc_msgSend)(
            saveRequest, @selector(saveSynchronouslyWithError:), &error
        );
        if (!saved) return Fail(@"save_failed", error, YES);

        error = nil;
        id restoredReminder = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(
            store, @selector(fetchReminderWithObjectID:error:), reminderObjectID, &error
        );
        if (!restoredReminder) return Fail(@"restore_readback_failed", error, YES);
        id restoredList = [restoredReminder respondsToSelector:@selector(list)]
            ? Send0(restoredReminder, @selector(list))
            : nil;
        NSString *restoredListID = ObjectUUID(restoredList);
        if (![restoredListID isEqualToString:listUUIDString]) {
            return Fail(@"restore_destination_mismatch", nil, YES);
        }
        NSUInteger attachmentCount = 0;
        if ([restoredReminder respondsToSelector:@selector(attachmentContext)]) {
            id context = Send0(restoredReminder, @selector(attachmentContext));
            if ([context respondsToSelector:@selector(attachments)]) {
                NSArray *attachments = Send0(context, @selector(attachments));
                attachmentCount = attachments.count;
            }
        }

        WriteJSON(@{
            @"ok": @YES,
            @"operation": @"recover_deleted_reminder",
            @"mutation_attempted": @YES,
            @"saved": @YES,
            @"pre_save_guard_matched": @YES,
            @"reminder_id": reminderUUIDString,
            @"destination_list_id": listUUIDString,
            @"attachment_count": @(attachmentCount)
        });
        return 0;
    }
}
