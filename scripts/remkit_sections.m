#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <dlfcn.h>

static id send_id(id target, SEL selector) {
    return ((id (*)(id, SEL))objc_msgSend)(target, selector);
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

static int fail(NSString *code, NSError *error, BOOL mutationAttempted) {
    NSMutableDictionary *payload = [@{
        @"ok": @NO,
        @"error": code,
        @"mutation_attempted": @(mutationAttempted)
    } mutableCopy];
    if (error.localizedDescription) {
        payload[@"detail"] = error.localizedDescription;
    }
    write_json(payload);
    return mutationAttempted ? 1 : 2;
}

static NSString *object_uuid(id object) {
    if (![object respondsToSelector:@selector(objectID)]) return nil;
    id objectID = send_id(object, @selector(objectID));
    if (![objectID respondsToSelector:@selector(uuid)]) return nil;
    NSUUID *uuid = send_id(objectID, @selector(uuid));
    return uuid.UUIDString;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc < 2) {
            return fail(@"usage", nil, NO);
        }

        void *reminderKit = dlopen(
            "/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit",
            RTLD_NOW
        );
        void *reminderKitInternal = dlopen(
            "/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal",
            RTLD_NOW
        );
        if (!reminderKit && !reminderKitInternal) {
            return fail(@"dlopen_failed", nil, NO);
        }

        Class REMStore = NSClassFromString(@"REMStore");
        Class REMList = NSClassFromString(@"REMList");
        Class REMReminder = NSClassFromString(@"REMReminder");
        Class REMListSection = NSClassFromString(@"REMListSection");
        Class REMSaveRequest = NSClassFromString(@"REMSaveRequest");
        Class REMMembership = NSClassFromString(@"REMMembership");
        Class REMMemberships = NSClassFromString(@"REMMemberships");
        if (!REMStore || !REMList || !REMReminder || !REMListSection || !REMSaveRequest
            || !REMMembership || !REMMemberships) {
            return fail(@"required_reminderkit_classes_missing", nil, NO);
        }

        id store = [[REMStore alloc] init];
        if (!store) {
            return fail(@"store_initialization_failed", nil, NO);
        }
        id saveRequest = ((id (*)(id, SEL, id))objc_msgSend)(
            [REMSaveRequest alloc], @selector(initWithStore:), store
        );
        if (!saveRequest || ![saveRequest respondsToSelector:@selector(setSyncToCloudKit:)]
            || ![saveRequest respondsToSelector:@selector(saveSynchronouslyWithError:)]) {
            return fail(@"required_reminderkit_selectors_missing", nil, NO);
        }
        ((void (*)(id, SEL, BOOL))objc_msgSend)(
            saveRequest, @selector(setSyncToCloudKit:), YES
        );

        NSString *operation = [NSString stringWithUTF8String:argv[1]];
        NSMutableDictionary *result = [@{
            @"ok": @NO,
            @"operation": operation,
            @"mutation_attempted": @NO
        } mutableCopy];
        NSError *error = nil;

        if ([operation isEqualToString:@"create"] && argc == 4) {
            NSString *listUUIDString = [NSString stringWithUTF8String:argv[2]];
            NSString *name = [NSString stringWithUTF8String:argv[3]];
            NSUUID *listUUID = [[NSUUID alloc] initWithUUIDString:listUUIDString];
            if (!listUUID || name.length == 0) {
                return fail(@"invalid_arguments", nil, NO);
            }
            id listObjectID = ((id (*)(id, SEL, id))objc_msgSend)(
                REMList, @selector(objectIDWithUUID:), listUUID
            );
            id list = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(
                store, @selector(fetchListWithObjectID:error:), listObjectID, &error
            );
            if (!list) {
                return fail(@"fetch_list_failed", error, NO);
            }
            id listChange = ((id (*)(id, SEL, id))objc_msgSend)(
                saveRequest, @selector(updateList:), list
            );
            id context = send_id(listChange, @selector(sectionsContextChangeItem));
            if (!context) {
                return fail(@"section_context_unavailable", nil, NO);
            }
            id sectionChange = ((id (*)(id, SEL, id, id))objc_msgSend)(
                saveRequest,
                @selector(addListSectionWithDisplayName:toListSectionContextChangeItem:),
                name,
                context
            );
            NSString *sectionUUID = object_uuid(sectionChange);
            if (!sectionChange || !sectionUUID) {
                return fail(@"create_section_failed", nil, NO);
            }
            result[@"list_id"] = listUUIDString;
            result[@"section_id"] = sectionUUID;
        } else if ([operation isEqualToString:@"repair"] && argc == 4) {
            NSString *sectionUUIDString = [NSString stringWithUTF8String:argv[2]];
            NSString *name = [NSString stringWithUTF8String:argv[3]];
            NSUUID *sectionUUID = [[NSUUID alloc] initWithUUIDString:sectionUUIDString];
            if (!sectionUUID || name.length == 0) {
                return fail(@"invalid_arguments", nil, NO);
            }
            id sectionObjectID = ((id (*)(id, SEL, id))objc_msgSend)(
                REMListSection, @selector(objectIDWithUUID:), sectionUUID
            );
            id section = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(
                store, @selector(fetchListSectionWithObjectID:error:), sectionObjectID, &error
            );
            if (!section) {
                return fail(@"fetch_section_failed", error, NO);
            }
            id sectionChange = ((id (*)(id, SEL, id))objc_msgSend)(
                saveRequest, @selector(updateListSection:), section
            );
            if (![sectionChange respondsToSelector:@selector(setDisplayName:)]) {
                return fail(@"section_display_name_selector_missing", nil, NO);
            }
            ((void (*)(id, SEL, id))objc_msgSend)(
                sectionChange, @selector(setDisplayName:), name
            );
            result[@"section_id"] = sectionUUIDString;
        } else if ([operation isEqualToString:@"move"] && argc == 4) {
            NSString *reminderUUIDString = [NSString stringWithUTF8String:argv[2]];
            NSString *sectionUUIDString = [NSString stringWithUTF8String:argv[3]];
            NSUUID *reminderUUID = [[NSUUID alloc] initWithUUIDString:reminderUUIDString];
            NSUUID *sectionUUID = [[NSUUID alloc] initWithUUIDString:sectionUUIDString];
            if (!reminderUUID || !sectionUUID) {
                return fail(@"invalid_arguments", nil, NO);
            }
            id reminderObjectID = ((id (*)(id, SEL, id))objc_msgSend)(
                REMReminder, @selector(objectIDWithUUID:), reminderUUID
            );
            id reminder = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(
                store, @selector(fetchReminderWithObjectID:error:), reminderObjectID, &error
            );
            if (!reminder) {
                return fail(@"fetch_reminder_failed", error, NO);
            }
            id list = send_id(reminder, @selector(list));
            id listChange = ((id (*)(id, SEL, id))objc_msgSend)(
                saveRequest, @selector(updateList:), list
            );
            id context = send_id(listChange, @selector(sectionsContextChangeItem));
            if (!context) {
                return fail(@"section_context_unavailable", nil, NO);
            }
            id membership = ((id (*)(id, SEL, id, id, BOOL, id))objc_msgSend)(
                [REMMembership alloc],
                @selector(initWithMemberIdentifier:groupIdentifier:isObsolete:modifiedOn:),
                reminderUUID,
                sectionUUID,
                NO,
                [NSDate date]
            );
            id memberships = ((id (*)(id, SEL, id))objc_msgSend)(
                [REMMemberships alloc], @selector(initWithMemberships:), @[membership]
            );
            ((void (*)(id, SEL, id))objc_msgSend)(
                context,
                @selector(setUnsavedMembershipsOfRemindersInSections:),
                memberships
            );
            result[@"reminder_id"] = reminderUUIDString;
            result[@"section_id"] = sectionUUIDString;
        } else {
            return fail(@"usage", nil, NO);
        }

        result[@"mutation_attempted"] = @YES;
        error = nil;
        BOOL saved = ((BOOL (*)(id, SEL, NSError **))objc_msgSend)(
            saveRequest, @selector(saveSynchronouslyWithError:), &error
        );
        if (!saved) {
            return fail(@"save_failed", error, YES);
        }
        result[@"ok"] = @YES;
        result[@"saved"] = @YES;
        write_json(result);
        return 0;
    }
}
