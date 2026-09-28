#ifndef QUICKFIX_STDAFX_H
#define QUICKFIX_STDAFX_H

#define WIN32_LEAN_AND_MEAN
#define _WINSOCK_DEPRECATED_NO_WARNINGS
#define _CRT_SECURE_NO_WARNINGS
#include "config.h"
#include <BaseTsd.h>
#include <stdio.h>

typedef SSIZE_T ssize_t;

#if _MSC_VER >= 1300
#define TERMINATE_IN_STD 1
#endif

#define _WIN32_DCOM

#endif
