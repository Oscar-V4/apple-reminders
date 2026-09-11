#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <dlfcn.h>
#import <CommonCrypto/CommonDigest.h>

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

// Early Reminder is a due-date delta context, never a REMAlarmTimeIntervalTrigger.
static id early_send1(id target, NSString *name, id value) {
    return ((id (*)(id, SEL, id))objc_msgSend)(target, NSSelectorFromString(name), value);
}

static NSString *early_digest(id value) {
    NSError *error = nil;
    NSData *data = [NSKeyedArchiver archivedDataWithRootObject:value requiringSecureCoding:NO error:&error];
    if (!data || error) return nil;
    unsigned char bytes[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, bytes);
    NSMutableString *result = [NSMutableString string];
    for (NSUInteger i = 0; i < sizeof(bytes); i++) [result appendFormat:@"%02x", bytes[i]];
    return result;
}

static NSDictionary *early_snapshot(id reminder) {
    id storage = [reminder valueForKey:@"storage"];
    id context = [reminder valueForKey:@"dueDateDeltaAlertContext"];
    NSArray *alerts = [context valueForKey:@"dueDateDeltaAlerts"];
    if (!storage || !context || ![alerts isKindOfClass:[NSArray class]] || alerts.count > 200) return nil;
    NSArray *units = @[@"minute", @"hour", @"day", @"week", @"month"];
    NSMutableArray *values = [NSMutableArray array];
    for (id alert in alerts) {
        id delta = [alert valueForKey:@"dueDateDelta"];
        NSNumber *unit = [delta valueForKey:@"unit"], *count = [delta valueForKey:@"count"];
        if (!unit || !count) return nil;
        NSInteger code = unit.integerValue, amount = count.integerValue;
        if (code < 0 || code >= (NSInteger)units.count || amount >= 0 || amount < -200) {
            [values addObject:@{@"read_only": @YES, @"unit_code": unit, @"count": count}];
        } else {
            [values addObject:@{@"unit": units[code], @"value": @(-amount)}];
        }
    }
    // Hash every reviewed user-owned native field independently. Preserve full alarm
    // objects and duplicate counts; provider revision/display metadata is excluded.
    NSArray *fields = @[@"objectID", @"listID", @"accountID", @"parentReminderID",
        @"titleDocumentData", @"notesDocumentData", @"titleAsString", @"notesAsString",
        @"icsUrl", @"userActivity", @"priority", @"completed", @"completionDate",
        @"flagged", @"allDay", @"timeZone", @"startDateComponents", @"dueDateComponents",
        @"recurrenceRules", @"alarms", @"attachments", @"hashtags", @"assignments",
        @"contactHandles", @"isUrgentStateEnabledForCurrentUser"];
    NSMutableArray *stable = [NSMutableArray array];
    for (NSString *field in fields) {
        id value = [storage valueForKey:field] ?: [NSNull null];
        if ([value isKindOfClass:[NSArray class]] || [value isKindOfClass:[NSSet class]]) {
            NSMutableArray *digests = [NSMutableArray array];
            for (id member in value) {
                NSString *digest = early_digest(member);
                if (!digest) return nil;
                [digests addObject:digest];
            }
            value = [digests sortedArrayUsingSelector:@selector(compare:)];
        }
        NSString *digest = early_digest(value);
        if (!digest) return nil;
        [stable addObject:@[field, digest]];
    }
    NSString *guard = early_digest(storage), *preserved = early_digest(stable);
    if (!guard || !preserved) return nil;
    return @{@"reminder_id": object_uuid(reminder) ?: @"", @"native_guard": guard,
        @"stable_digest": preserved, @"early_reminders": values,
        @"early_reminder": values.count == 1 ? values[0] : [NSNull null],
        @"early_reminder_count": @(values.count)};
}

static id early_fetch(id store, NSUUID *uuid, NSError **error) {
    Class optionsClass = NSClassFromString(@"REMReminderFetchOptions");
    id options = [[optionsClass alloc] init];
    SEL include = NSSelectorFromString(@"setIncludeDueDateDeltaAlerts:");
    SEL fetch = NSSelectorFromString(@"fetchReminderWithObjectID:fetchOptions:error:");
    if (![options respondsToSelector:include] || ![store respondsToSelector:fetch]) return nil;
    ((void (*)(id, SEL, BOOL))objc_msgSend)(options, include, YES);
    id objectID = early_send1(NSClassFromString(@"REMReminder"), @"objectIDWithUUID:", uuid);
    return ((id (*)(id, SEL, id, id, NSError **))objc_msgSend)(store, fetch, objectID, options, error);
}

static int early_main(int argc, const char **argv, NSString *operation) {
    BOOL mutation = [operation isEqualToString:@"early-set"];
    if ((mutation && argc != 5) || (!mutation && argc != 3)) return fail(@"invalid_arguments", nil, NO);
    NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:@(argv[2])];
    NSString *expectedGuard = mutation ? @(argv[3]) : nil;
    id desired = mutation ? [NSJSONSerialization JSONObjectWithData:[@(argv[4]) dataUsingEncoding:NSUTF8StringEncoding]
        options:NSJSONReadingFragmentsAllowed error:nil] : nil;
    NSArray *units = @[@"minute", @"hour", @"day", @"week", @"month"];
    if (!uuid || (mutation && (expectedGuard.length != 64 || !desired))) return fail(@"invalid_arguments", nil, NO);
    if (mutation && desired != [NSNull null]) {
        if (![desired isKindOfClass:[NSDictionary class]] || [desired count] != 2
            || ![units containsObject:desired[@"unit"]] || ![desired[@"value"] isKindOfClass:[NSNumber class]]
            || CFGetTypeID((__bridge CFTypeRef)desired[@"value"]) == CFBooleanGetTypeID()
            || [desired[@"value"] doubleValue] != [desired[@"value"] integerValue]
            || [desired[@"value"] integerValue] < 1 || [desired[@"value"] integerValue] > 200)
            return fail(@"invalid_arguments", nil, NO);
    }
    BOOL attempted = NO;
    @try {
        Class storeClass = NSClassFromString(@"REMStore");
        id store = [[storeClass alloc] init]; NSError *error = nil;
        id reminder = early_fetch(store, uuid, &error);
        if (!reminder) return fail(@"native_read_failed", error, NO);
        NSDictionary *before = early_snapshot(reminder);
        if (!before) return fail(@"native_guard_unavailable", nil, NO);
        if (!mutation) {
            write_json(@{@"ok": @YES, @"operation": operation, @"mutation_attempted": @NO, @"snapshot": before});
            return 0;
        }
        if (![before[@"native_guard"] isEqual:expectedGuard]) return fail(@"concurrent_modification", nil, NO);
        if ([before[@"early_reminder_count"] integerValue] > 1
            || [before[@"early_reminder"] isKindOfClass:[NSDictionary class]] && before[@"early_reminder"][@"read_only"])
            return fail(@"unsupported_early_reminder", nil, NO);
        if (desired != [NSNull null] && ![reminder valueForKey:@"dueDateComponents"])
            return fail(@"early_reminder_requires_due", nil, NO);
        if ([before[@"early_reminder"] isEqual:desired]) {
            write_json(@{@"ok": @YES, @"operation": operation, @"mutation_attempted": @NO,
                @"saved": @NO, @"matched": @YES, @"before": before, @"after": before});
            return 0;
        }
        id save = early_send1([NSClassFromString(@"REMSaveRequest") alloc], @"initWithStore:", store);
        SEL sync = NSSelectorFromString(@"setSyncToCloudKit:"), saveSEL = NSSelectorFromString(@"saveSynchronouslyWithError:");
        if (![save respondsToSelector:sync] || ![save respondsToSelector:saveSEL]) return fail(@"required_reminderkit_selectors_missing", nil, NO);
        ((void (*)(id, SEL, BOOL))objc_msgSend)(save, sync, YES);
        id change = early_send1(save, @"updateReminder:", reminder);
        id context = [change valueForKey:@"dueDateDeltaAlertContext"];
        SEL remove = NSSelectorFromString(@"removeAllFetchedDueDateDeltaAlerts");
        SEL add = NSSelectorFromString(@"addDueDateDeltaAlertWithDueDateDelta:");
        if (![context respondsToSelector:remove] || ![context respondsToSelector:add]) return fail(@"required_reminderkit_selectors_missing", nil, NO);
        id delta = nil;
        if (desired != [NSNull null]) {
            id allocated = [NSClassFromString(@"REMDueDateDeltaInterval") alloc];
            SEL init = NSSelectorFromString(@"initWithUnit:count:");
            if (![allocated respondsToSelector:init]) return fail(@"required_reminderkit_selectors_missing", nil, NO);
            delta = ((id (*)(id, SEL, NSInteger, NSInteger))objc_msgSend)(allocated, init,
                [units indexOfObject:desired[@"unit"]], -[desired[@"value"] integerValue]);
            if (!delta) return fail(@"invalid_native_delta", nil, NO);
        }
        // Re-read with a separate store immediately before constructing the save.
        id latest = early_fetch([[storeClass alloc] init], uuid, &error);
        NSDictionary *latestSnapshot = latest ? early_snapshot(latest) : nil;
        if (![latestSnapshot[@"native_guard"] isEqual:expectedGuard]) return fail(@"concurrent_modification", nil, NO);
        ((void (*)(id, SEL))objc_msgSend)(context, remove);
        if (delta && !((id (*)(id, SEL, id))objc_msgSend)(context, add, delta)) return fail(@"invalid_native_delta", nil, NO);
        attempted = YES;
        BOOL saved = ((BOOL (*)(id, SEL, NSError **))objc_msgSend)(save, saveSEL, &error);
        if (!saved) return fail(@"save_failed", error, YES);
        id final = early_fetch([[storeClass alloc] init], uuid, &error);
        NSDictionary *after = final ? early_snapshot(final) : nil;
        BOOL matched = after && [after[@"stable_digest"] isEqual:before[@"stable_digest"]]
            && [after[@"early_reminder"] isEqual:desired]
            && [after[@"early_reminder_count"] integerValue] == (desired == [NSNull null] ? 0 : 1);
        write_json(@{@"ok": @YES, @"operation": operation, @"mutation_attempted": @YES,
            @"saved": @YES, @"matched": @(matched), @"before": before, @"after": after ?: @{},
            @"pre_save_guard_matched": @YES});
        return 0;
    } @catch (NSException *exception) {
        return fail(@"native_early_reminder_exception", nil, attempted);
    }
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

        NSString *earlyOperation = @(argv[1]);
        if ([earlyOperation isEqualToString:@"early-read"] || [earlyOperation isEqualToString:@"early-set"])
            return early_main(argc, argv, earlyOperation);

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
